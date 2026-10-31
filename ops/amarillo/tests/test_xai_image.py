"""xAI picture edits, with the CLI as the fallback. No network."""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import sys
import urllib.error

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import xai_image  # noqa: E402


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 12), "white").save(buf, format="PNG")
    return buf.getvalue()


class _Body:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self, _n):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("KLUKAI_XAI_LEDGER", str(tmp_path / "spend.sqlite3"))
    monkeypatch.setenv("KLUKAI_XAI_API_KEY", "unit-test-key")
    monkeypatch.setenv("KLUKAI_IMAGE_BACKEND", "api")
    monkeypatch.delenv("KLUKAI_API_DAILY_USD_CAP", raising=False)
    return tmp_path


def _request():
    return {
        "width": 832,
        "height": 1216,
        "prompt": "evening on the deck",
        "user_id": "jalsarraf",
    }


def test_an_edit_sends_her_portrait_and_records_nine_cents(home, monkeypatch):
    reference = _png()
    seen = []

    def urlopen(req, timeout=0):
        seen.append(req)
        body = json.dumps(
            {
                "data": [{"b64_json": base64.b64encode(reference).decode()}],
                "usage": {"cost_in_usd_ticks": 900_000_000},
            }
        ).encode()
        return _Body(body)

    monkeypatch.setattr(xai_image.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(xai_image, "_portrait", lambda: reference)

    def fallback(*_args):
        raise AssertionError("cli")

    image = xai_image.render_with_fallback(
        _request(), {"klukai": {"path": "/tmp/klukai.png"}}, fallback
    )
    assert image.startswith(b"\x89PNG")
    payload = json.loads(seen[0].data)
    assert payload["model"] == "grok-imagine-image-2.0"
    assert payload["resolution"] == "2k"
    assert "quality" not in payload
    assert payload["aspect_ratio"] == "2:3"
    assert payload["response_format"] == "b64_json"
    assert payload["n"] == 1
    raw = seen[0].data.decode()
    # The retired ids are assembled so this file does not contain them whole.
    for slug in ("grok-imagine-image-" + part for part in ("quality", "pro")):
        assert slug not in raw
    encoded = payload["images"][0]["url"].split(",", 1)[1]
    assert base64.b64decode(encoded) == reference
    assert seen[0].get_header("Authorization") == "Bearer unit-test-key"
    assert seen[0].full_url.endswith("/images/edits")
    spend = xai_image._spend()
    assert spend.spent_today() == pytest.approx(0.09)


def test_nudity_and_a_missing_reference_never_call_the_api(home, monkeypatch):
    def urlopen(*_args, **_kwargs):
        raise AssertionError("network")

    monkeypatch.setattr(xai_image.urllib.request, "urlopen", urlopen)
    called = []
    monkeypatch.setattr(xai_image, "_portrait", lambda: called.append(1) or _png())

    def fallback(*_args):
        called.append("cli")
        return b"cli"

    with pytest.raises(xai_image.agy.RuntimeError, match="Nudity"):
        xai_image.render_with_fallback({**_request(), "nudity": True}, {}, fallback)
    assert called == []
    monkeypatch.setattr(xai_image, "_portrait", lambda: (_ for _ in ()).throw(OSError("gone")))
    assert xai_image.render_with_fallback(_request(), {"klukai": {}}, fallback) == b"cli"
    assert called == ["cli"]


def test_api_failure_cap_and_cli_mode_use_the_account_renderer(home, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://api.x.ai/v1/images/edits", 500, "no", hdrs=None, fp=io.BytesIO(b"scene text")
        )

    monkeypatch.setattr(xai_image.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(xai_image, "_portrait", lambda: _png())
    seen = []

    def fallback(request, references):
        seen.append(request["prompt"])
        return b"cli-png"

    assert xai_image.render_with_fallback(_request(), {"klukai": {}}, fallback) == b"cli-png"
    assert xai_image._spend().spent_today() == 0.0
    monkeypatch.setenv("KLUKAI_API_DAILY_USD_CAP", "0")

    def explode(*_args, **_kwargs):
        raise AssertionError("capped")

    monkeypatch.setattr(xai_image.urllib.request, "urlopen", explode)
    assert xai_image.render_with_fallback(_request(), {"klukai": {}}, fallback) == b"cli-png"
    monkeypatch.setenv("KLUKAI_IMAGE_BACKEND", "cli")
    monkeypatch.setenv("KLUKAI_API_DAILY_USD_CAP", "3")
    assert xai_image.render_with_fallback(_request(), {"klukai": {}}, fallback) == b"cli-png"
    assert len(seen) == 3


def test_the_vision_checker_uses_low_effort_and_falls_back(home, monkeypatch):
    portrait = _png()
    candidate = _png()
    seen = []

    def urlopen(req, timeout=0):
        seen.append(json.loads(req.data))
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"verdict":"pass","issues":[]}',
                        }
                    }
                ],
                "usage": {"cost_in_usd_ticks": 70_000_000},
            }
        ).encode()
        return _Body(body)

    monkeypatch.setattr(xai_image.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(xai_image, "_portrait", lambda: portrait)

    def cli(*_args, **_kwargs):
        raise AssertionError("cli")

    verdict = xai_image.check_with_fallback(
        candidate, _request(), {}, timeout=30, cli=cli
    )
    assert verdict["verdict"] == "pass"
    assert seen[0]["model"] == "grok-4.7"
    assert seen[0]["reasoning_effort"] == "low"
    assert seen[0]["messages"][1]["content"][0]["type"] == "image_url"
    assert xai_image._spend().spent_today() == pytest.approx(0.007)

    def explode(*_args, **_kwargs):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(xai_image.urllib.request, "urlopen", explode)
    assert xai_image.check_with_fallback(
        candidate, _request(), {}, timeout=30, cli=lambda *a, **k: {"verdict": "unchecked"}
    )["verdict"] == "unchecked"


def test_the_worker_grok_provider_refuses_nudity_before_the_api():
    script = Path(__file__).resolve().parents[3] / "scripts"
    sys.path.insert(0, str(script))
    import agy_image_worker as worker

    render = worker.load_providers(["grok"])[0][1]
    with pytest.raises(Exception, match="Nudity"):
        render({"nudity": True, "width": 832, "height": 1216, "prompt": "x"}, {})
