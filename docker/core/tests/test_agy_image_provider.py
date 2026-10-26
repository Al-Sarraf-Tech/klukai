"""Explicit chat images use only the bounded, authenticated Unix worker."""

import asyncio
import io
import json
from uuid import UUID
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo


def png_bytes(size=(64, 48), *, fmt="PNG", metadata=False):
    output = io.BytesIO()
    info = PngInfo()
    if metadata:
        info.add_text("Comment", "private-provider-metadata")
    Image.new("RGB", size, "green").save(output, format=fmt, pnginfo=info)
    return output.getvalue()


@pytest.fixture
def agy_config(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("test-private-agy-token\n")
    token.chmod(0o600)
    monkeypatch.setenv("KLUKAI_CHAT_IMAGE_PROVIDER", "agy")
    monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", "jalsarraf, test-user")
    monkeypatch.setenv("KLUKAI_AGY_SOCKET", str(tmp_path / "image.sock"))
    monkeypatch.setenv("KLUKAI_AGY_TOKEN_FILE", str(token))
    return token


def worker_api(monkeypatch, handler):
    from app import agy_image_provider as agy

    original = httpx.AsyncClient

    def transport(**kwargs):
        assert kwargs["uds"].endswith("/image.sock")
        assert kwargs["retries"] == 0
        assert kwargs["trust_env"] is False
        return httpx.MockTransport(handler)

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"].read <= 380
        return original(**kwargs)

    monkeypatch.setattr(agy.httpx, "AsyncHTTPTransport", transport)
    monkeypatch.setattr(agy.httpx, "AsyncClient", client)


@pytest.mark.parametrize(("provider", "allowed", "user_id", "expected"), [
    ("", "jalsarraf", "jalsarraf", False),
    ("comfyui", "jalsarraf", "jalsarraf", False),
    ("agy", "", "jalsarraf", False),
    ("agy", "jalsarraf", "other", False),
    ("agy", "jalsarraf", "jalsarraf-extra", False),
    ("agy", "jalsarraf, test-user", "test-user", True),
])
def test_selection_requires_explicit_provider_and_exact_user(monkeypatch, provider, allowed, user_id, expected):
    from app import agy_image_provider as agy

    monkeypatch.setenv("KLUKAI_CHAT_IMAGE_PROVIDER", provider)
    monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", allowed)
    assert agy.uses_agy_images(user_id) is expected


def test_an_unset_image_provider_is_the_account_worker(monkeypatch):
    """selected_provider() reads an unset KLUKAI_IMAGE_PROVIDER as "agy"; the
    worker gate must agree, or every picture is refused while routed to it."""
    from app import agy_image_provider as agy
    from app.image_provider import selected_provider

    monkeypatch.delenv("KLUKAI_IMAGE_PROVIDER", raising=False)
    monkeypatch.delenv("KLUKAI_CHAT_IMAGE_PROVIDER", raising=False)
    monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", "jalsarraf")
    assert selected_provider() == "agy"
    assert agy.uses_agy_images("jalsarraf") is True
    assert agy.uses_agy_images("tj") is False


@pytest.mark.parametrize("outcome", ["complete", "error", "cancel"])
async def test_recovery_holds_busy_wait_marker_until_finished(agy_config, monkeypatch, outcome):
    from app import agy_image_provider as agy

    entered, release = asyncio.Event(), asyncio.Event()
    paths = []

    async def recover(path, payload):
        paths.append(path)
        entered.set()
        await release.wait()
        if outcome == "error":
            raise httpx.ReadError("interrupted recovery")
        return {"status": "pending", "image": None}

    monkeypatch.setattr(agy, "_post_worker", recover)
    assert not agy.generation_in_flight()
    task = asyncio.create_task(agy.recover_agy_image(
        "rooftop garden", user_id="jalsarraf", width=832, height=1216,
        include_commander=False, request_id="bdcda13d-c025-4b50-bcea-7debe28cc482",
    ))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert agy.generation_in_flight(), "new images must wait while recovery reviews an existing image"
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            result = await task
            assert result["status"] == ("unknown" if outcome == "error" else "pending")
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert not agy.generation_in_flight()
    assert paths == ["/recover"]


async def test_posts_authenticated_identity_once_and_strips_output_metadata(agy_config, monkeypatch):
    from app import agy_image_provider as agy

    requests = []

    async def handler(request):
        requests.append(request)
        assert request.method == "POST" and request.url.path == "/generate"
        assert request.headers["authorization"] == "Bearer test-private-agy-token"
        payload = json.loads(await request.aread())
        assert str(UUID(payload.pop("request_id")))
        assert payload == {
            "user_id": "jalsarraf", "prompt": "Explicit scene", "width": 1216,
            "height": 832, "include_commander": True,
        }
        # Worker output dimensions may differ from the requested orientation.
        return httpx.Response(200, headers={"content-type": "image/png"}, content=png_bytes(metadata=True))

    worker_api(monkeypatch, handler)
    result = await agy.generate_agy_image("Explicit scene", user_id="jalsarraf", width=1216, height=832, include_commander=True)
    assert len(requests) == 1
    assert b"private-provider-metadata" not in result
    with Image.open(io.BytesIO(result)) as image:
        image.load()
        assert image.size == (64, 48) and image.format == "PNG" and image.info == {}


@pytest.mark.parametrize("status", [302, 400, 401, 403, 409, 429, 502, 504])
async def test_worker_failure_never_retries_redirects_or_logs_private_data(agy_config, monkeypatch, caplog, status):
    from app import agy_image_provider as agy

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"location": "https://private.invalid/image"}, text="test-private-agy-token secret scene")

    worker_api(monkeypatch, handler)
    assert await agy.generate_agy_image("secret scene", user_id="jalsarraf") is None
    assert len(requests) == 1
    assert "secret scene" not in caplog.text and "test-private-agy-token" not in caplog.text


@pytest.mark.parametrize("kind", ["wrong-type", "json-url", "corrupt", "truncated", "crc", "jpeg", "animation", "pixel-limit", "byte-limit", "normalized-limit"])
async def test_invalid_images_fail_closed(agy_config, monkeypatch, kind):
    from app import agy_image_provider as agy

    raw = png_bytes()
    content_type = "image/png"
    if kind == "wrong-type":
        content_type = "text/html"
    elif kind == "json-url":
        raw = b'{"url":"https://private.invalid/image.png"}'
    elif kind == "corrupt":
        raw = b"not an image"
    elif kind == "truncated":
        raw = raw[:50]
    elif kind == "crc":
        damaged = bytearray(raw)
        damaged[-13] ^= 1
        raw = bytes(damaged)
    elif kind == "jpeg":
        raw = png_bytes(fmt="JPEG")
    elif kind == "animation":
        output = io.BytesIO()
        Image.new("RGB", (4, 4), "red").save(output, format="PNG", save_all=True, append_images=[Image.new("RGB", (4, 4), "blue")])
        raw = output.getvalue()
    elif kind == "pixel-limit":
        monkeypatch.setattr(agy, "MAX_IMAGE_PIXELS", 100)
    elif kind == "byte-limit":
        monkeypatch.setattr(agy, "MAX_IMAGE_BYTES", len(raw) - 1)
    elif kind == "normalized-limit":
        # A palette input expands when normalized into metadata-free RGB pixels.
        output = io.BytesIO()
        Image.new("1", (512, 512)).save(output, format="PNG")
        raw = output.getvalue()
        monkeypatch.setattr(agy, "MAX_IMAGE_BYTES", len(raw) + 1)
    worker_api(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": content_type}, content=raw))
    assert await agy.generate_agy_image("scene", user_id="jalsarraf") is None


@pytest.mark.parametrize("change", ["user", "disabled", "empty-prompt", "long-prompt", "blank-prompt", "zero-width", "bool-width", "large-size", "missing-token", "public-token", "empty-token", "relative-socket", "relative-token"])
async def test_invalid_request_or_config_never_contacts_worker(agy_config, monkeypatch, change):
    from app import agy_image_provider as agy

    kwargs = {"user_id": "jalsarraf"}
    prompt = "scene"
    if change == "user":
        kwargs["user_id"] = "other"
    elif change == "disabled":
        monkeypatch.setenv("KLUKAI_CHAT_IMAGE_PROVIDER", "")
    elif change == "empty-prompt":
        prompt = ""
    elif change == "long-prompt":
        prompt = "x" * 16_001
    elif change == "blank-prompt":
        prompt = "  "
    elif change == "zero-width":
        kwargs["width"] = 0
    elif change == "bool-width":
        kwargs["width"] = True
    elif change == "large-size":
        kwargs.update(width=4000, height=4000)
    elif change == "missing-token":
        agy_config.unlink()
    elif change == "public-token":
        agy_config.chmod(0o644)
    elif change == "empty-token":
        agy_config.write_text("")
    elif change == "relative-socket":
        monkeypatch.setenv("KLUKAI_AGY_SOCKET", "image.sock")
    elif change == "relative-token":
        monkeypatch.setenv("KLUKAI_AGY_TOKEN_FILE", "token")
    worker_api(monkeypatch, lambda request: pytest.fail("invalid request contacted worker"))
    assert await agy.generate_agy_image(prompt, **kwargs) is None


async def test_absolute_deadline_cancels_slow_request_without_retry(agy_config, monkeypatch):
    from app import agy_image_provider as agy

    requests = []

    async def handler(request):
        requests.append(request)
        await asyncio.sleep(5)
        pytest.fail("deadline escaped")

    monkeypatch.setattr(agy, "REQUEST_DEADLINE_SECONDS", 0.01)
    worker_api(monkeypatch, handler)
    assert await agy.generate_agy_image("scene", user_id="jalsarraf") is None
    assert len(requests) == 1


async def test_transport_error_is_generic_and_not_retried(agy_config, monkeypatch, caplog):
    from app import agy_image_provider as agy

    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("test-private-agy-token secret scene", request=request)

    worker_api(monkeypatch, handler)
    assert await agy.generate_agy_image("secret scene", user_id="jalsarraf") is None
    assert len(calls) == 1
    assert "secret scene" not in caplog.text and "test-private-agy-token" not in caplog.text


async def test_global_agy_dispatch_preserves_identity_and_never_acquires_gpu(agy_config, monkeypatch):
    from app import agy_image_provider as agy, image_gen

    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "agy")
    monkeypatch.setenv("KLUKAI_CHAT_IMAGE_PROVIDER", "")
    monkeypatch.setattr(image_gen, "_leased", AsyncMock(side_effect=AssertionError("GPU lease")))
    requests = []

    async def handler(request):
        payload = json.loads(await request.aread())
        requests.append(payload)
        assert payload["user_id"] == "test-user" and payload["include_commander"] is False
        assert "silver hair" in payload["prompt"] and "fully clothed" in payload["prompt"]
        assert "red hair" in payload["prompt"]
        return httpx.Response(200, headers={"content-type": "image/png"}, content=png_bytes())

    worker_api(monkeypatch, handler)
    assert agy.uses_agy_images("test-user")
    result = await image_gen.generate_image("silver hair", user_id="test-user", sfw=True, negative_extra="red hair")
    assert result.startswith(b"\x89PNG") and len(requests) == 1


@pytest.mark.parametrize("user_id", [None, "other"])
async def test_global_agy_missing_or_unauthorized_user_cannot_fall_back(agy_config, monkeypatch, user_id):
    from app import image_gen

    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "agy")
    monkeypatch.setattr(image_gen, "_leased", AsyncMock(side_effect=AssertionError("GPU fallback")))
    worker_api(monkeypatch, lambda request: pytest.fail("unauthorized worker call"))
    assert await image_gen.generate_image("scene", user_id=user_id) is None


@pytest.mark.parametrize("provider", ["agy", "disabled", "openai", "invalid"])
async def test_nonlocal_provider_blocks_all_legacy_gpu_image_entrypoints(monkeypatch, provider):
    from app import image_gen

    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", provider)
    monkeypatch.setattr(image_gen, "gpu_lease", lambda *a, **kw: pytest.fail("GPU image lease"))
    assert await image_gen.generate_img2img(b"source", "portrait", denoise=0.2, seed=1) is None
    assert await image_gen.generate_inpaint_set(base_prompt="portrait", base_png=None, plan=lambda *a: [], seed=1) == {}
    assert await image_gen.free_comfyui_vram() is True
    if provider != "agy":
        assert await image_gen.check_comfyui_ready() is False


@pytest.mark.parametrize("payload,expected", [
    ({"status": "ready", "provider": "agy", "busy": False}, True),
    ({"status": "ready", "provider": "agy", "busy": True}, False),
    ({"status": "down", "provider": "agy", "busy": False}, False),
    ({"status": "ready", "provider": "other", "busy": False}, False),
])
async def test_global_image_readiness_uses_authenticated_unix_health(agy_config, monkeypatch, payload, expected):
    from app import image_gen

    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "agy")
    monkeypatch.setattr(image_gen, "gpu_lease", lambda *a, **kw: pytest.fail("GPU readiness"))

    def handler(request):
        assert request.method == "GET" and request.url.path == "/health"
        assert request.headers["authorization"] == "Bearer test-private-agy-token"
        return httpx.Response(200, json=payload)

    worker_api(monkeypatch, handler)
    assert await image_gen.check_comfyui_ready() is expected


def test_agy_background_jobs_use_durable_remote_generation_guards(monkeypatch):
    from app import memory_her_pov

    monkeypatch.setenv("KLUKAI_CREATIVE_TEXT_PROVIDER", "local")
    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "agy")
    assert memory_her_pov._cloud_generation_enabled()


async def test_real_unix_socket_transport_ignores_proxy_environment(agy_config, monkeypatch):
    from app import agy_image_provider as agy

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    body = png_bytes()
    requests = []

    async def worker(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        lines = headers.decode().split("\r\n")
        length = int(next(line.split(":", 1)[1] for line in lines if line.lower().startswith("content-length:")))
        requests.append((headers, json.loads(await reader.readexactly(length))))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(worker, path=agy_config.parent / "image.sock")
    async with server:
        result = await agy.generate_agy_image("scene", user_id="test-user")
    assert result.startswith(b"\x89PNG")
    assert len(requests) == 1
    assert requests[0][1]["user_id"] == "test-user"
    assert b"Authorization: Bearer test-private-agy-token" in requests[0][0]


def test_only_the_owner_may_carry_the_commander_reference():
    from app import agy_image_provider as agy

    assert agy.commander_reference_allowed("jalsarraf") is True
    for user in ("tj", "claude", "", None, "jalsarraf-extra"):
        assert agy.commander_reference_allowed(user) is False


async def test_refusal_for_a_user_off_the_list_is_logged(monkeypatch, caplog):
    from app import agy_image_provider as agy

    monkeypatch.setenv("KLUKAI_CHAT_IMAGE_PROVIDER", "agy")
    monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", "jalsarraf")
    payload = await agy._validated_payload(
        "draw her", user_id="tj", width=832, height=1216, include_commander=False, request_id=None,
    )
    assert payload is None
    assert "Picture refused: tj is not on KLUKAI_AGY_ALLOWED_USERS" in caplog.text
