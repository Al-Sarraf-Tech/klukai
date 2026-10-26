"""Offline contracts for opt-in, separately capped reference-edit APIs."""

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import http.client
import importlib
import io
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import urllib.error
import urllib.request
import urllib.response

import pytest
from PIL import Image, PngImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import agy_art_runtime as agy  # noqa: E402

REQUEST = {
    "request_id": "f4bc9fbd-bd54-4cb7-9762-8015626d6737",
    "user_id": "claude",
    "prompt": "adult woman in a rooftop garden wearing a trench coat",
    "width": 832, "height": 1216, "include_commander": False,
}
KEY = "unit-key-not-a-credential"


def png(color="green", size=(832, 1216)):
    output = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("fixture", "remove source metadata")
    Image.new("RGB", size, color).save(output, format="PNG", pnginfo=metadata)
    return output.getvalue()


def setup(tmp_path, monkeypatch):
    try:
        cloud = importlib.import_module("cloud_image_render")
    except ModuleNotFoundError:
        pytest.fail("The optional reference-edit adapters are not implemented")
    for provider in ("OPENAI", "GEMINI"):
        monkeypatch.setenv(f"KLUKAI_{provider}_IMAGE_DAILY_LIMIT", "10")
        monkeypatch.setenv(f"KLUKAI_{provider}_IMAGE_MONTHLY_LIMIT", "150")
        monkeypatch.setenv(f"KLUKAI_{provider}_IMAGE_LEDGER", str(tmp_path / "attempts.sqlite3"))
    reads = []

    def decrypt(name):
        reads.append(name)
        return f'OPENAI_API_KEY="{KEY}"\nGEMINI_API_KEY={KEY}\n'.encode()

    monkeypatch.setattr(cloud, "_secret_store", lambda: SimpleNamespace(decrypt=decrypt))
    reference = tmp_path / "klukai.png"
    reference.write_bytes(png("blue", (64, 96)))
    references = {"klukai": {"path": str(reference), "sha256": hashlib.sha256(reference.read_bytes()).hexdigest()},
                  "commander": {"path": "/never-read-private-portrait", "sha256": "invalid"}}
    return cloud, references, reads


def attempts(tmp_path, provider):
    with closing(sqlite3.connect(tmp_path / "attempts.sqlite3")) as db:
        return db.execute("SELECT COUNT(*) FROM image_attempts WHERE provider = ?", (provider,)).fetchone()[0]


def payload(provider, raw=None):
    raw = raw or png(size=(832, 1216) if provider == "openai" else (768, 1152))
    encoded = base64.b64encode(raw).decode()
    if provider == "openai":
        return {"created": 1793000000, "data": [{"b64_json": encoded}], "usage": {"total_tokens": 100}}
    return {"id": "test-interaction", "status": "completed", "model": "gemini-nano-banana-2.1",
            "steps": [{"type": "model_output", "content": [{"type": "image", "mime_type": "image/png", "data": encoded}]}],
            "usage": {"total_tokens": 100}}


def transport(monkeypatch, cloud, result, check=None, error=None):
    calls = []

    def send(request, timeout):
        calls.append(request)
        assert 0 < timeout <= 150
        if check:
            check(request)
        if error:
            raise error
        return io.BytesIO(json.dumps(result).encode())

    monkeypatch.setattr(cloud, "_urlopen", send)
    return calls


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_reference_edit_contract_is_one_paid_post_with_normalized_image(tmp_path, monkeypatch, provider):
    cloud, refs, reads = setup(tmp_path, monkeypatch)

    def check(request):
        assert request.get_method() == "POST"
        assert attempts(tmp_path, provider) == 1
        body = json.loads(request.data)
        if provider == "openai":
            assert request.full_url == "https://api.openai.com/v1/images/edits"
            assert request.get_header("Authorization") == f"Bearer {KEY}"
            assert body["model"] == "gpt-image-2.5-sunburst-2026-09-08"
            assert body["n"] == 1 and body["quality"] == "max" and body["size"] == "832x1216"
            assert body["output_format"] == "png"
            assert len(body["images"]) == 1
            assert body["images"][0]["image_url"].startswith("data:image/png;base64,")
            encoded_reference = body["images"][0]["image_url"].split(",", 1)[1]
            prompt = body["prompt"]
        else:
            assert request.full_url == "https://generativelanguage.googleapis.com/v1beta/interactions"
            assert request.get_header("X-goog-api-key") == KEY
            assert body["model"] == "gemini-nano-banana-2.1"
            assert body["store"] is False and body["background"] is False and body["stream"] is False
            assert "tools" not in body
            assert body["response_format"] == {"type": "image", "delivery": "inline", "aspect_ratio": "2:3", "image_size": "2K"}
            assert len(body["input"]) == 2 and body["input"][1]["type"] == "image"
            encoded_reference = body["input"][1]["data"]
            prompt = body["input"][0]["text"]
        with Image.open(io.BytesIO(base64.b64decode(encoded_reference))) as reference:
            assert reference.size == (64, 96) and reference.getpixel((0, 0)) == (0, 0, 255)
            assert reference.info == {}
        assert prompt.startswith("2D anime illustration")
        assert "green eyes" in prompt and "NOT photorealistic" in prompt
        assert REQUEST["prompt"] in prompt and "Only Klukai" in prompt

    calls = transport(monkeypatch, cloud, payload(provider), check=check)
    result = getattr(cloud, f"render_{provider}")(REQUEST, refs)
    assert reads == ["compose-env"] and len(calls) == 1
    with Image.open(io.BytesIO(result)) as image:
        assert image.format == "PNG" and image.getpixel((0, 0)) == (0, 128, 0)
        assert image.info == {}


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_commander_uses_only_text_and_the_approved_klukai_image(tmp_path, monkeypatch, provider):
    cloud, refs, _ = setup(tmp_path, monkeypatch)
    calls = transport(monkeypatch, cloud, payload(provider))
    getattr(cloud, f"render_{provider}")(dict(REQUEST, user_id="jalsarraf", include_commander=True), refs)
    serialized = calls[0].data.decode()
    assert "The adult human Commander" in serialized and "short dark crew-cut hair" in serialized
    assert "/never-read-private-portrait" not in serialized


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("patch", [{"nudity": True}, {"include_commander": True}, {"include_commander": "yes"},
                                   {"width": 0}, {"width": True}, {"height": 1217}, {"width": 4096},
                                   {"width": 256, "height": 1024}, {"prompt": ""}, {"prompt": []}])
def test_invalid_or_unsupported_request_fails_before_private_work(tmp_path, monkeypatch, provider, patch):
    cloud, refs, reads = setup(tmp_path, monkeypatch)
    calls = transport(monkeypatch, cloud, payload(provider))
    with pytest.raises(agy.RuntimeError):
        getattr(cloud, f"render_{provider}")(dict(REQUEST, **patch), refs)
    assert not calls and not reads and not (tmp_path / "attempts.sqlite3").exists()


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("period,value", [("DAILY", None), ("DAILY", "0"), ("MONTHLY", "0"), ("MONTHLY", "bad")])
def test_unconfigured_caps_stop_before_reference_and_secret_reads(tmp_path, monkeypatch, provider, period, value):
    cloud, refs, reads = setup(tmp_path, monkeypatch)
    name = f"KLUKAI_{provider.upper()}_IMAGE_{period}_LIMIT"
    monkeypatch.delenv(name) if value is None else monkeypatch.setenv(name, value)
    monkeypatch.setattr(cloud, "read_image", lambda entry: pytest.fail("Reference read while disabled"))
    calls = transport(monkeypatch, cloud, payload(provider))
    with pytest.raises(agy.ProviderUnavailable):
        getattr(cloud, f"render_{provider}")(REQUEST, refs)
    assert not calls and not reads


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_missing_encrypted_key_does_not_use_environment_or_reserve_spend(tmp_path, monkeypatch, provider):
    cloud, refs, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", KEY)
    monkeypatch.setattr(cloud, "_secret_store", lambda: SimpleNamespace(decrypt=lambda name: b"OTHER=value\n"))
    calls = transport(monkeypatch, cloud, payload(provider))
    with pytest.raises(agy.ProviderUnavailable):
        getattr(cloud, f"render_{provider}")(REQUEST, refs)
    assert not calls and not (tmp_path / "attempts.sqlite3").exists()


def test_daily_monthly_caps_are_atomic_persistent_and_provider_specific(tmp_path, monkeypatch):
    cloud, _, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setenv("KLUKAI_OPENAI_IMAGE_DAILY_LIMIT", "3")
    monkeypatch.setenv("KLUKAI_OPENAI_IMAGE_MONTHLY_LIMIT", "3")

    def reserve(_):
        try:
            cloud.reserve_attempt("openai", REQUEST["request_id"])
            return True
        except agy.ProviderUnavailable:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(reserve, range(8))) == 3
    assert attempts(tmp_path, "openai") == 3
    # An independent provider retains its separate allowance, even sharing a DB.
    cloud.reserve_attempt("gemini", REQUEST["request_id"])
    assert attempts(tmp_path, "gemini") == 1
    monkeypatch.setenv("KLUKAI_OPENAI_IMAGE_DAILY_LIMIT", "10")
    with pytest.raises(agy.ProviderUnavailable):
        cloud.reserve_attempt("openai", REQUEST["request_id"])
    assert (tmp_path / "attempts.sqlite3").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("error", [TimeoutError("sensitive prompt"), urllib.error.URLError("secret body")])
def test_uncertain_submission_never_retries_or_permits_failover(tmp_path, monkeypatch, provider, error):
    cloud, refs, _ = setup(tmp_path, monkeypatch)
    calls = transport(monkeypatch, cloud, None, error=error)
    with pytest.raises(agy.RuntimeError) as caught:
        getattr(cloud, f"render_{provider}")(REQUEST, refs)
    assert not isinstance(caught.value, agy.ProviderUnavailable)
    assert "uncertain" in str(caught.value).lower()
    assert "sensitive" not in str(caught.value) and "secret" not in str(caught.value)
    assert len(calls) == 1 and attempts(tmp_path, provider) == 1


@pytest.mark.parametrize("provider,code,kind", [("openai", "moderation_blocked", agy.ContentRefused),
                                                ("gemini", "image_safety", agy.ContentRefused),
                                                ("gemini", "content_blocked", agy.ContentRefused),
                                                ("gemini", "no_image", agy.RuntimeError)])
def test_typed_refusal_precedes_service_status_and_hides_provider_message(tmp_path, monkeypatch, provider, code, kind):
    cloud, refs, _ = setup(tmp_path, monkeypatch)
    error = urllib.error.HTTPError("https://provider.invalid", 503, "private prompt", {},
                                   io.BytesIO(json.dumps({"error": {"code": code, "message": KEY}}).encode()))
    calls = transport(monkeypatch, cloud, None, error=error)
    with pytest.raises(kind) as caught:
        getattr(cloud, f"render_{provider}")(REQUEST, refs)
    assert not isinstance(caught.value, agy.ProviderUnavailable)
    assert KEY not in str(caught.value) and "private prompt" not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("status,unavailable", [(401, True), (429, True), (408, False), (500, False), (307, False)])
def test_http_errors_are_bounded_and_do_not_retry(tmp_path, monkeypatch, provider, status, unavailable):
    cloud, refs, _ = setup(tmp_path, monkeypatch)
    error = urllib.error.HTTPError("https://provider.invalid", status, KEY, {}, io.BytesIO(b"not JSON"))
    calls = transport(monkeypatch, cloud, None, error=error)
    with pytest.raises(agy.RuntimeError) as caught:
        getattr(cloud, f"render_{provider}")(REQUEST, refs)
    assert isinstance(caught.value, agy.ProviderUnavailable) is unavailable
    assert KEY not in str(caught.value) and len(calls) == 1


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("bad", [None, [], {}, {"data": [{"url": "https://private.invalid/image"}]},
                               {"status": "completed", "steps": []}])
def test_missing_malformed_and_url_only_outputs_are_rejected(tmp_path, monkeypatch, provider, bad):
    cloud, refs, _ = setup(tmp_path, monkeypatch)
    calls = transport(monkeypatch, cloud, bad)
    with pytest.raises(agy.RuntimeError):
        getattr(cloud, f"render_{provider}")(REQUEST, refs)
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("kind", ["invalid-image", "reference", "wrong-dimensions"])
def test_decode_checks_image_pixels_and_rejects_reference_echo(tmp_path, monkeypatch, provider, kind):
    cloud, refs, _ = setup(tmp_path, monkeypatch)
    raw = b"not an image" if kind == "invalid-image" else png("blue", (64, 96)) if kind == "reference" else png(size=(64, 64))
    if kind == "reference":
        # Match valid output dimensions so removing the echo guard breaks this test.
        raw = png("blue", (832, 1216) if provider == "openai" else (768, 1152))
        Path(refs["klukai"]["path"]).write_bytes(raw)
        refs["klukai"]["sha256"] = hashlib.sha256(raw).hexdigest()
    transport(monkeypatch, cloud, payload(provider, raw))
    with pytest.raises(agy.RuntimeError):
        getattr(cloud, f"render_{provider}")(REQUEST, refs)


def test_transport_rejects_redirect_before_forwarding_auth(tmp_path, monkeypatch):
    cloud, refs, _ = setup(tmp_path, monkeypatch)
    urls = []

    def https_open(handler, request):
        urls.append(request.full_url)
        headers = {"location": "https://credential-thief.invalid/collect"}
        response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 307)
        response.msg = "Temporary Redirect"
        return response

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    with pytest.raises(agy.RuntimeError):
        cloud.render_openai(REQUEST, refs)
    assert urls == ["https://api.openai.com/v1/images/edits"]


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_normalized_reference_over_transport_limit_is_rejected_before_spend(tmp_path, monkeypatch, provider):
    cloud, refs, reads = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(cloud, "read_image", lambda entry: b"x" * (12 * 1024 * 1024 + 1))
    calls = transport(monkeypatch, cloud, payload(provider))
    with pytest.raises(agy.RuntimeError):
        getattr(cloud, f"render_{provider}")(REQUEST, refs)
    assert not reads and not calls and not (tmp_path / "attempts.sqlite3").exists()


def test_broken_error_body_still_has_sanitized_uncertain_result(tmp_path, monkeypatch):
    cloud, refs, _ = setup(tmp_path, monkeypatch)

    class BrokenBody(io.BytesIO):
        def read(self, *args):
            raise http.client.IncompleteRead(b"private body", 50)

    error = urllib.error.HTTPError("https://provider.invalid", 503, KEY, {}, BrokenBody())
    calls = transport(monkeypatch, cloud, None, error=error)
    with pytest.raises(agy.RuntimeError) as caught:
        cloud.render_openai(REQUEST, refs)
    assert not isinstance(caught.value, agy.ProviderUnavailable)
    assert "private body" not in str(caught.value) and len(calls) == 1
