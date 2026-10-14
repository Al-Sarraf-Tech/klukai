"""Cloud images are opt-in, bounded, reference-based, and never use the GPU."""

from __future__ import annotations

import base64
import asyncio
import io
import json
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from app import image_gen


def image_bytes(size=(832, 1216), fmt="PNG"):
    out = io.BytesIO()
    Image.new("RGB", size, "green").save(out, format=fmt)
    return out.getvalue()


@pytest.fixture
def configured(monkeypatch, tmp_path):
    from app import image_provider

    monkeypatch.setattr(image_provider, "_cloud_render_lock", asyncio.Semaphore(1))
    ref = tmp_path / "reference.png"
    ref.write_bytes(image_bytes((64, 64)))
    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-image-key")
    monkeypatch.setenv("KLUKAI_IMAGE_REFERENCE_FILES", json.dumps([str(ref)]))
    monkeypatch.setenv("KLUKAI_CLOUD_IMAGE_DAILY_LIMIT", "10")
    monkeypatch.setenv("KLUKAI_CLOUD_IMAGE_MONTHLY_LIMIT", "20")
    monkeypatch.setenv("KLUKAI_CREATIVE_LEDGER", str(tmp_path / "attempts.sqlite3"))
    monkeypatch.delenv("KLUKAI_OPENAI_IMAGE_MODEL", raising=False)
    monkeypatch.delenv("KLUKAI_OPENAI_IMAGE_QUALITY", raising=False)
    return ref


def fake_api(monkeypatch, handler):
    from app import image_provider

    original = httpx.AsyncClient
    clients = []

    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"].read <= 360
        client = original(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(image_provider.httpx, "AsyncClient", factory)
    return clients


def attempts():
    import os

    with closing(sqlite3.connect(os.environ["KLUKAI_CREATIVE_LEDGER"])) as conn:
        return conn.execute("SELECT COUNT(*) FROM creative_attempts WHERE kind='image'").fetchone()[0]


async def test_cloud_dispatch_never_enters_local_lease_when_key_missing(monkeypatch):
    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def forbidden(*args, **kwargs):
        pytest.fail("Cloud selection touched Dominus")

    monkeypatch.setattr(image_gen, "_leased", forbidden)
    assert await image_gen.generate_image("portrait") is None


async def test_default_still_uses_local_renderer(monkeypatch):
    monkeypatch.delenv("KLUKAI_IMAGE_PROVIDER", raising=False)
    monkeypatch.setattr(image_gen, "_leased", AsyncMock(return_value=b"local"))
    assert await image_gen.generate_image("portrait") == b"local"


async def test_unknown_provider_fails_closed(monkeypatch):
    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "openia")
    monkeypatch.setattr(image_gen, "_leased", AsyncMock(side_effect=AssertionError("local fallback")))
    assert await image_gen.generate_image("portrait") is None


async def test_reference_edit_request_and_png_result(configured, monkeypatch):
    from app import image_provider

    requests = []

    async def handler(request):
        requests.append(request)
        assert attempts() == 1  # Durable reservation precedes outbound HTTP.
        assert str(request.url) == "https://api.openai.com/v1/images/edits"
        assert request.headers["authorization"] == "Bearer test-image-key"
        body = await request.aread()
        for value in [b"gpt-image-2.5-sunburst-2026-09-08", b"max", b"832x1216", b"image[]", b"image/png"]:
            assert value in body
        assert b'name="n"\r\n\r\n1\r\n' in body
        assert b"green eyes" in body and b"black gloves" in body
        assert b"<lora:" not in body and b":1.4" not in body
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(image_bytes(fmt="JPEG")).decode()}], "usage": {"total_tokens": 100}})

    clients = fake_api(monkeypatch, handler)
    monkeypatch.setattr(image_gen, "_leased", AsyncMock(side_effect=AssertionError("Dominus")))
    assert image_provider.cloud_images_ready()
    result = await image_gen.generate_image("<lora:Klukai:0.9>, (green eyes:1.4), black gloves", retry=True)
    assert len(requests) == 1
    assert result.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (832, 1216)
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("name,value", [
    ("KLUKAI_IMAGE_REFERENCE_FILES", "[]"),
    ("KLUKAI_IMAGE_REFERENCE_FILES", '["relative.png"]'),
    ("KLUKAI_IMAGE_REFERENCE_FILES", "not-json"),
    ("KLUKAI_IMAGE_REFERENCE_FILES", '["/missing.png"]'),
    ("KLUKAI_OPENAI_IMAGE_MODEL", "gpt-image-latest"),
    ("KLUKAI_OPENAI_IMAGE_QUALITY", "auto"),
    ("OPENAI_API_KEY", ""),
])
async def test_invalid_configuration_never_sends(configured, monkeypatch, name, value):
    from app import image_provider

    monkeypatch.setenv(name, value)
    fake_api(monkeypatch, lambda request: pytest.fail("invalid config sent HTTP"))
    assert not image_provider.cloud_images_ready()
    assert await image_gen.generate_image("portrait") is None


@pytest.mark.parametrize("size", [(512, 512), (833, 1216), (4000, 1024), (3840, 3840), (0, 1024), (True, 1024)])
async def test_invalid_dimensions_never_send(configured, monkeypatch, size):
    fake_api(monkeypatch, lambda request: pytest.fail("invalid dimensions sent HTTP"))
    assert await image_gen.generate_image("portrait", width=size[0], height=size[1]) is None


async def test_corrupt_reference_never_sends(configured, monkeypatch):
    from app import image_provider

    configured.write_bytes(b"not an image")
    fake_api(monkeypatch, lambda request: pytest.fail("corrupt reference sent HTTP"))
    assert not image_provider.cloud_images_ready()
    assert await image_gen.generate_image("portrait") is None


async def test_too_many_references_never_send(configured, monkeypatch):
    monkeypatch.setenv("KLUKAI_IMAGE_REFERENCE_FILES", json.dumps([str(configured)] * 17))
    fake_api(monkeypatch, lambda request: pytest.fail("too many references sent HTTP"))
    assert await image_gen.generate_image("portrait") is None


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"location": "https://other.invalid/leak"}),
    httpx.Response(429, json={"error": {"message": "test-image-key private prompt"}}),
    httpx.Response(500),
    httpx.Response(200, json={"data": [{"url": "https://other.invalid/image"}]}),
    httpx.Response(200, json={"data": [{"b64_json": "%%%"}]}),
    httpx.Response(200, json={"data": [{"b64_json": "bm90IGFuIGltYWdl"}]}),
    httpx.Response(200, json={"data": []}),
    httpx.Response(200, json={"data": [None]}),
    httpx.Response(200, content=b"[" * 100_000 + b"]" * 100_000),
])
async def test_provider_failure_consumes_one_attempt_without_retry(configured, monkeypatch, response, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return response

    fake_api(monkeypatch, handler)
    monkeypatch.setattr(image_gen, "_leased", AsyncMock(side_effect=AssertionError("fallback")))
    assert await image_gen.generate_image("private prompt", retry=True) is None
    assert len(calls) == 1 and attempts() == 1
    assert "private prompt" not in caplog.text and "test-image-key" not in caplog.text


async def test_timeout_consumes_attempt_without_retry(configured, monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("private prompt and test-image-key", request=request)

    fake_api(monkeypatch, handler)
    assert await image_gen.generate_image("portrait") is None
    assert attempts() == 1


async def test_exhausted_budget_propagates_without_network(configured, monkeypatch):
    from app.creative_budget import CreativeBudgetError

    monkeypatch.setenv("KLUKAI_CLOUD_IMAGE_DAILY_LIMIT", "0")
    fake_api(monkeypatch, lambda request: pytest.fail("disabled budget sent HTTP"))
    with pytest.raises(CreativeBudgetError):
        await image_gen.generate_image("portrait")


async def test_response_byte_limit_is_enforced_while_streaming(configured, monkeypatch):
    from app import image_provider

    monkeypatch.setattr(image_provider, "MAX_RESPONSE_BYTES", 32)
    fake_api(monkeypatch, lambda request: httpx.Response(200, content=b" " * 33))
    assert await image_gen.generate_image("portrait") is None
    assert attempts() == 1


def test_cloud_prompt_preserves_identity_and_outfit_without_diffusion_markup():
    from app.image_provider import cloud_prompt

    text = cloud_prompt("masterpiece, 1girl, hk416 \\(girls' frontline\\), ((silver hair:1.2)), green eyes, <lora:private_file:1>, teardrop facial mark, cross hair ornament, black gloves, blue_dress", sfw=True, negative_extra="red hair")
    for detail in ["silver hair", "green eyes", "teardrop facial mark", "cross hair ornament", "black gloves", "blue dress", "girls' frontline"]:
        assert detail in text
    assert "lora" not in text and "private_file" not in text and ":1.2" not in text
    assert "one woman" in text and "fully clothed" in text
    assert "red hair" in text


async def test_img2img_cannot_fall_back_to_gpu_when_cloud_selected(configured, monkeypatch):
    monkeypatch.setattr(image_gen, "gpu_lease", lambda *a, **kw: pytest.fail("cloud mode acquired GPU"))
    fake_api(monkeypatch, lambda request: pytest.fail("portrait sent cloud HTTP"))
    assert await image_gen.generate_img2img(b"source", "blink", denoise=0.2, seed=42) is None


async def test_cloud_renders_serialize_before_reference_loading(configured, monkeypatch):
    from app import image_provider

    entered = asyncio.Event()
    release = asyncio.Event()
    loaded = []
    original_references = image_provider._references

    def references():
        loaded.append(True)
        return original_references()

    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(500)

    monkeypatch.setattr(image_provider, "_references", references)
    fake_api(monkeypatch, handler)
    first = asyncio.create_task(image_gen.generate_image("one"))
    await entered.wait()
    second = asyncio.create_task(image_gen.generate_image("two"))
    await asyncio.sleep(0.02)
    try:
        assert len(loaded) == 1
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert attempts() == 2


async def test_final_wrapped_prompt_cannot_exceed_api_limit(configured, monkeypatch):
    fake_api(monkeypatch, lambda request: pytest.fail("oversized wrapped prompt sent HTTP"))
    assert await image_gen.generate_image("x" * 31_999) is None


async def test_reference_metadata_is_not_uploaded(configured, monkeypatch):
    from PIL.PngImagePlugin import PngInfo

    metadata = PngInfo()
    metadata.add_text("Comment", "private-reference-metadata")
    Image.new("RGB", (64, 64), "green").save(configured, pnginfo=metadata)

    async def handler(request):
        assert b"private-reference-metadata" not in await request.aread()
        return httpx.Response(500)

    fake_api(monkeypatch, handler)
    assert await image_gen.generate_image("portrait") is None


async def test_absolute_deadline_stops_dripping_response(configured, monkeypatch):
    from app import image_provider

    async def handler(request):
        await asyncio.sleep(5)
        pytest.fail("request escaped absolute deadline")

    monkeypatch.setattr(image_provider, "REQUEST_DEADLINE_SECONDS", 0.01)
    fake_api(monkeypatch, handler)
    assert await image_gen.generate_image("portrait") is None
    assert attempts() == 1


async def test_busy_renderer_stops_before_reserving_budget(configured, monkeypatch):
    from app import image_provider

    monkeypatch.setattr(image_provider, "QUEUE_WAIT_SECONDS", 0.01, raising=False)
    fake_api(monkeypatch, lambda request: pytest.fail("queued request sent HTTP"))
    async with image_provider._cloud_render_lock:
        assert await asyncio.wait_for(image_gen.generate_image("portrait"), timeout=0.1) is None
    assert not (configured.parent / "attempts.sqlite3").exists()


async def test_flare_snapshot_and_usage_record_failure_preserve_result(configured, monkeypatch):
    from app import creative_budget

    monkeypatch.setenv("KLUKAI_OPENAI_IMAGE_MODEL", "gpt-image-2.5-flare-2026-09-08")
    monkeypatch.setattr(creative_budget, "record_usage", AsyncMock(side_effect=OSError("private error")))

    async def handler(request):
        assert b"gpt-image-2.5-flare-2026-09-08" in await request.aread()
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(image_bytes()).decode()}], "usage": {"total_tokens": 100}})

    fake_api(monkeypatch, handler)
    assert (await image_gen.generate_image("portrait")).startswith(b"\x89PNG")


@pytest.mark.parametrize("which", ["reference-bytes", "reference-pixels", "reference-total", "output-size"])
async def test_image_bounds_reject_invalid_payloads(configured, monkeypatch, which):
    from app import image_provider

    if which == "reference-bytes":
        monkeypatch.setattr(image_provider, "MAX_REFERENCE_BYTES", 16)
    elif which == "reference-pixels":
        monkeypatch.setattr(image_provider, "MAX_REFERENCE_PIXELS", 16)
    elif which == "reference-total":
        monkeypatch.setattr(image_provider, "MAX_REFERENCE_TOTAL_BYTES", 16)

    def handler(request):
        assert which == "output-size"
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(image_bytes((1024, 1024))).decode()}]})

    fake_api(monkeypatch, handler)
    assert await image_gen.generate_image("portrait") is None
