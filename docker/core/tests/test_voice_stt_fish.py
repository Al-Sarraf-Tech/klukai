"""Fish Audio speech-to-text: the ASR client and the ``/api/stt`` route.

``transcribe-1-pro`` is pay-as-you-go, so no test reaches Fish: every request
lands on an ``httpx.MockTransport`` that plays its documented replies.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app import voice_client
from app.voice_client import SpeechToTextError

KEY = "fish-stt-secret-key-do-not-log"
TAKE = b"\x1aE\xdf\xa3" + b"opus-take-bytes" * 4
TAKE_B64 = base64.b64encode(TAKE).decode()
SPOKEN = "Commander reporting in from the east wing"


@pytest.fixture(autouse=True)
def fish(monkeypatch):
    monkeypatch.setenv("KLUKAI_VOICE_ENABLED", "true")
    monkeypatch.setenv("FISH_API_KEY", KEY)


class _Fish:
    """Patches ``httpx.AsyncClient`` onto a MockTransport playing Fish."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []
        self.built: list[dict[str, Any]] = []
        self._real = httpx.AsyncClient

    def _handle(self, request: httpx.Request):
        self.requests.append(request)
        return self.handler(request)

    def _factory(self, **kwargs: Any) -> httpx.AsyncClient:
        self.built.append(kwargs)
        return self._real(transport=httpx.MockTransport(self._handle), **kwargs)

    def __enter__(self) -> _Fish:
        self._patch = patch("httpx.AsyncClient", self._factory)
        self._patch.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._patch.stop()


def _reply(status: int = 200, **kwargs: Any):
    return lambda _request: httpx.Response(status, **kwargs)


def _ok(**extra: Any):
    body = {"text": f"  {SPOKEN}  ", "duration": 2.5, "language": "English",
            "language_code": "en", "segments": [], **extra}
    return _reply(200, json=body)


def _form(request: httpx.Request) -> dict[str, bytes]:
    """The multipart fields of a request, by name."""
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    fields: dict[str, bytes] = {}
    for part in request.content.split(b"--" + boundary):
        head, sep, value = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        name = re.search(rb'name="([^"]+)"', head)
        assert name is not None
        fields[name.group(1).decode()] = value.removesuffix(b"\r\n")
    return fields


# ── The ASR request ───────────────────────────────────────────────────────


class TestRequest:
    async def test_endpoint_headers_form_and_dedicated_client(self):
        with _Fish(_ok()) as fish:
            result = await voice_client.transcribe(TAKE_B64)

        assert result == {"text": SPOKEN, "language": "en", "duration": 2.5}
        [request] = fish.requests
        assert request.method == "POST"
        assert str(request.url) == "https://api.fish.audio/v1/asr"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert request.headers["model"] == "transcribe-1-pro"
        assert request.headers["content-type"].startswith("multipart/form-data")
        form = _form(request)
        assert form["audio"] == TAKE
        assert form["ignore_timestamps"] == b"true"
        assert form["tag_audio_events"] == b"false"
        assert form["diarize"] == b"false"
        assert "language" not in form
        assert "model" not in form  # the model belongs in the header
        # A dedicated client: no proxies from the environment, no redirects.
        assert fish.built == [
            {"timeout": 30.0, "trust_env": False, "follow_redirects": False}
        ]

    @pytest.mark.parametrize(
        ("given", "sent"),
        [("en", b"en"), ("ja", b"ja"), (" JA ", b"ja"), ("fr", None), ("", None),
         (None, None)],
    )
    async def test_language_is_an_en_or_ja_hint(self, given, sent):
        with _Fish(_ok()) as fish:
            await voice_client.transcribe(TAKE_B64, language=given)
        assert _form(fish.requests[0]).get("language") == sent

    async def test_redirects_are_not_followed(self):
        moved = _reply(307, headers={"Location": "https://elsewhere.test/asr"})
        with _Fish(moved) as fish:
            with pytest.raises(SpeechToTextError) as failed:
                await voice_client.transcribe(TAKE_B64)
        assert len(fish.requests) == 1
        assert failed.value.status == 502


class TestNotConfigured:
    @pytest.mark.parametrize("key", [None, "", "   "])
    async def test_no_key_sends_nothing(self, monkeypatch, key):
        if key is None:
            monkeypatch.delenv("FISH_API_KEY")
        else:
            monkeypatch.setenv("FISH_API_KEY", key)
        assert voice_client.fish_stt_configured() is False
        assert voice_client.voice_input_enabled() is False
        with patch("httpx.AsyncClient", side_effect=AssertionError("client built")):
            with pytest.raises(SpeechToTextError) as failed:
                await voice_client.transcribe(TAKE_B64)
        assert (failed.value.status, failed.value.message) == (503, "Voice input is off")

    def test_switches(self, monkeypatch):
        assert voice_client.voice_input_enabled() is True
        monkeypatch.setenv("KLUKAI_VOICE_ENABLED", "off")
        assert voice_client.voice_input_enabled() is False


class TestAudio:
    @pytest.mark.parametrize("audio", ["abc", "é" * 8, "%%%%"])
    async def test_unreadable_base64_is_400_and_not_sent(self, audio):
        with _Fish(_ok()) as fish:
            with pytest.raises(SpeechToTextError) as failed:
                await voice_client.transcribe(audio)
        assert (failed.value.status, failed.value.message) == (400, "Invalid audio")
        assert fish.requests == []

    async def test_oversized_take_is_413_and_not_sent(self, monkeypatch):
        monkeypatch.setattr(voice_client, "MAX_STT_AUDIO_BYTES", len(TAKE) - 1)
        with _Fish(_ok()) as fish:
            with pytest.raises(SpeechToTextError) as failed:
                await voice_client.transcribe(TAKE_B64)
        assert failed.value.status == 413
        assert fish.requests == []


# ── Fish's errors ─────────────────────────────────────────────────────────


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "code", "mapped"),
        [
            (400, "audio_too_long", (502, "Transcription failed")),
            (400, "audio_too_short", (502, "Transcription failed")),
            (400, "invalid_audio", (502, "Transcription failed")),
            (413, "request_too_large", (502, "Transcription failed")),
            (415, "unsupported_media_type", (502, "Transcription failed")),
            (401, None, (502, "Voice service unavailable")),
            (402, None, (502, "Voice service unavailable")),
            (403, "upstream_rejected", (502, "Voice service unavailable")),
            (429, None, (503, "Voice service busy")),
            (500, "internal_error", (502, "Voice service unavailable")),
            (503, "upstream_unavailable", (502, "Voice service unavailable")),
        ],
    )
    async def test_status_mapping_never_echoes_the_body(self, status, code, mapped):
        body: dict[str, Any] = {"status": status, "message": "acct_123 secret detail"}
        if code:
            body["code"] = code
        with _Fish(_reply(status, json=body)) as fish:
            with pytest.raises(SpeechToTextError) as failed:
                await voice_client.transcribe(TAKE_B64)
        assert (failed.value.status, failed.value.message) == mapped
        assert "acct_123" not in str(failed.value)
        assert len(fish.requests) == 1  # never retried: each take is billed

    async def test_documented_codes_are_logged_and_nothing_else(self, caplog):
        caplog.set_level(logging.DEBUG)
        replies = [
            _reply(400, json={"code": "audio_too_short", "message": "acct_123"}),
            _reply(400, json={"code": "made_up_code_acct_123"}),
            _reply(400, json={"code": ["unhashable"]}),
            _reply(400, json=["not", "an", "object"]),
            _reply(413, content=b"<html>edge says acct_123</html>"),
            _reply(500, content=b"x" * 10_000),
        ]
        for reply in replies:
            with _Fish(reply):
                with pytest.raises(SpeechToTextError):
                    await voice_client.transcribe(TAKE_B64)
        assert "HTTP 400 (audio_too_short)" in caplog.text
        assert "HTTP 400 (-)" in caplog.text
        assert "HTTP 413 (-)" in caplog.text
        assert "acct_123" not in caplog.text

    @pytest.mark.parametrize(
        "error",
        [httpx.ReadTimeout("slow"), httpx.ConnectError("down"), httpx.RemoteProtocolError("x")],
        ids=["read-timeout", "connect", "protocol"],
    )
    async def test_transport_failures_are_503(self, error):
        def fail(request):
            raise error

        with _Fish(fail):
            with pytest.raises(SpeechToTextError) as failed:
                await voice_client.transcribe(TAKE_B64)
        assert (failed.value.status, failed.value.message) == (
            503,
            "Voice service unavailable",
        )

    async def test_the_whole_call_has_a_deadline(self, monkeypatch):
        monkeypatch.setattr(voice_client, "FISH_STT_TIMEOUT", 0.05)

        async def stall(request):
            await asyncio.sleep(5)
            return httpx.Response(200, json={"text": "late"})

        with _Fish(stall):
            with pytest.raises(SpeechToTextError) as failed:
                await voice_client.transcribe(TAKE_B64)
        assert failed.value.status == 503

    async def test_open_breaker_is_503_without_a_request(self):
        from app.circuit_breakers import get_breaker

        get_breaker("voice")._trip()
        with _Fish(_ok()) as fish:
            with pytest.raises(SpeechToTextError) as failed:
                await voice_client.transcribe(TAKE_B64)
        assert failed.value.status == 503
        assert fish.requests == []


# ── Fish's 200 ────────────────────────────────────────────────────────────


class TestReply:
    async def test_oversized_reply_is_refused(self, monkeypatch):
        monkeypatch.setattr(voice_client, "MAX_STT_RESPONSE_BYTES", 64)
        with _Fish(_reply(200, json={"text": "x" * 500})):
            with pytest.raises(SpeechToTextError) as failed:
                await voice_client.transcribe(TAKE_B64)
        assert (failed.value.status, failed.value.message) == (502, "Transcription failed")

    @pytest.mark.parametrize(
        "content",
        [
            b"not json",
            b"\xff\xfe",
            b'["text"]',
            b'{"duration": 1.0}',
            b'{"text": null}',
            b'{"text": 42}',
            b"[" * 200_000,
        ],
        ids=["text", "bad-utf8", "list", "no-text", "null-text", "int-text", "nested"],
    )
    async def test_a_reply_without_a_transcript_is_502(self, content):
        with _Fish(_reply(200, content=content)):
            with pytest.raises(SpeechToTextError) as failed:
                await voice_client.transcribe(TAKE_B64)
        assert (failed.value.status, failed.value.message) == (502, "Transcription failed")

    async def test_silence_is_an_empty_transcript(self):
        with _Fish(_reply(200, json={"text": "", "duration": 0.4})):
            assert await voice_client.transcribe(TAKE_B64) == {
                "text": "",
                "duration": 0.4,
            }

    @pytest.mark.parametrize(
        ("extra", "expected"),
        [
            ({"language_code": "JA"}, {"language": "ja"}),
            ({"language_code": "english"}, {}),
            ({"language_code": "e1"}, {}),
            ({"language_code": "日本"}, {}),
            ({"language_code": None}, {}),
            ({"duration": 3}, {"duration": 3.0}),
            ({"duration": -1}, {}),
            ({"duration": True}, {}),
            ({"duration": "2.5"}, {}),
        ],
    )
    async def test_optional_fields_are_validated(self, extra, expected):
        body = {"text": "hi", **extra}
        with _Fish(_reply(200, json=body)):
            assert await voice_client.transcribe(TAKE_B64) == {"text": "hi", **expected}

    async def test_non_finite_duration_is_dropped(self):
        with _Fish(_reply(200, content=b'{"text": "hi", "duration": NaN}')):
            assert await voice_client.transcribe(TAKE_B64) == {"text": "hi"}


class TestNoSecretsInLogs:
    async def test_key_audio_and_transcript_never_logged(self, caplog):
        caplog.set_level(logging.DEBUG)
        outcomes = [
            _ok(),
            _reply(401, json={"message": f"bad key {KEY}"}),
            _reply(400, json={"code": "invalid_audio", "message": SPOKEN}),
            _reply(200, content=f'{{"text": "{SPOKEN}"'.encode()),
        ]
        for outcome in outcomes:
            with _Fish(outcome):
                try:
                    await voice_client.transcribe(TAKE_B64, language="en")
                except SpeechToTextError:
                    pass
        assert caplog.records, "expected the failures to be logged"
        for secret in (KEY, SPOKEN, TAKE_B64, "opus-take-bytes"):
            assert secret not in caplog.text


# ── /api/stt ──────────────────────────────────────────────────────────────


def _app(user: str | None = "claude") -> FastAPI:
    from app.routes_voice_input import register_voice_input

    async def resolve(request):
        return user if request.headers.get("Authorization") == "Bearer good" else None

    app = FastAPI()
    register_voice_input(app, resolve)
    return app


async def _post(app: FastAPI, body: dict[str, Any], *, fish=None, auth=True):
    """POST /api/stt through ASGI; Fish (if given) is patched only meanwhile."""
    headers = {"Authorization": "Bearer good"} if auth else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://core.test"
    ) as client:
        if fish is None:
            return await client.post("/api/stt", json=body, headers=headers)
        with fish:
            return await client.post("/api/stt", json=body, headers=headers)


class TestRoute:
    async def test_login_required_and_nothing_sent(self):
        transcribe = AsyncMock(side_effect=AssertionError("transcribed"))
        with patch("app.voice_client.transcribe", transcribe):
            response = await _post(_app(), {"audio": TAKE_B64}, auth=False)
        assert response.status_code == 401

    async def test_transcript_contract(self):
        fish = _Fish(_ok())
        response = await _post(_app(), {"audio": TAKE_B64}, fish=fish)
        assert response.status_code == 200
        assert response.json() == {"text": SPOKEN, "language": "en", "duration": 2.5}
        assert "language" not in _form(fish.requests[0])

    async def test_language_hint_reaches_fish(self):
        fish = _Fish(_ok())
        response = await _post(_app(), {"audio": TAKE_B64, "language": "ja"}, fish=fish)
        assert response.status_code == 200
        assert _form(fish.requests[0])["language"] == b"ja"

    async def test_voice_off(self, monkeypatch):
        monkeypatch.setenv("KLUKAI_VOICE_ENABLED", "false")
        fish = _Fish(_ok())
        response = await _post(_app(), {"audio": TAKE_B64}, fish=fish)
        assert response.status_code == 503
        assert response.json() == {"error": "Voice is turned off"}
        assert fish.requests == []

    async def test_fish_not_configured(self, monkeypatch):
        monkeypatch.delenv("FISH_API_KEY")
        fish = _Fish(_ok())
        response = await _post(_app(), {"audio": TAKE_B64}, fish=fish)
        assert response.status_code == 503
        assert response.json() == {"error": "Voice input is off"}
        assert fish.requests == []

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            # A Fish 401 must never reach the apps as a 401: they log him out.
            (401, (502, "Voice service unavailable")),
            (400, (502, "Transcription failed")),
            (429, (503, "Voice service busy")),
        ],
    )
    async def test_fish_errors_map_to_safe_responses(self, status, expected):
        fish = _Fish(_reply(status, json={"message": "acct_123"}))
        response = await _post(_app(), {"audio": TAKE_B64}, fish=fish)
        assert (response.status_code, response.json()) == (
            expected[0],
            {"error": expected[1]},
        )
        assert b"acct_123" not in response.content

    async def test_bad_base64_is_400(self):
        response = await _post(_app(), {"audio": "abc"})
        assert response.status_code == 400
        assert response.json() == {"error": "Invalid audio"}

    async def test_unexpected_failure_is_503(self, caplog):
        boom = AsyncMock(side_effect=RuntimeError(f"broke on {SPOKEN}"))
        with patch("app.voice_client.transcribe", boom):
            response = await _post(_app(), {"audio": TAKE_B64})
        assert response.status_code == 503
        assert response.json() == {"error": "Voice service unavailable"}
        assert SPOKEN not in caplog.text

    def test_request_model(self):
        from app.api_schemas import STTRequest

        assert STTRequest(audio="AAAA").language is None
        assert STTRequest(audio="AAAA", language="ja").language == "ja"
        with pytest.raises(ValidationError):
            STTRequest(audio="AAAA", language="x" * 17)

    def test_registered_on_the_real_app(self):
        from app.routes import register_routes

        app = FastAPI()
        register_routes(app)
        assert any(
            getattr(r, "path", None) == "/api/stt" and "POST" in r.methods
            for r in app.routes
        )


_LOCAL_VOICE = (
    "VOICE_URL",
    "VOICE_STT_URL",
    "VOICE_BACKEND",
    "VOICE_API_TOKEN",
    "amarillo-xpu",
    "gpu_lease",
    "get_lm_gate",
    "100.107.121.5",
    ":8301",
)


@pytest.mark.parametrize(
    "module",
    ["voice_client", "routes_voice_input", "voice_jobs", "voice_archive", "chat", "routes"],
)
def test_no_local_voice_backend_remains(module):
    """The Dominus/Arc voice paths are gone from every voice module."""
    import importlib
    from pathlib import Path

    source = Path(importlib.import_module(f"app.{module}").__file__).read_text()
    for name in _LOCAL_VOICE:
        assert name not in source
    assert json.dumps(sorted(voice_client.FISH_LANGUAGES)) == '["en", "ja"]'


def test_core_is_not_given_a_local_voice_service():
    from pathlib import Path

    compose = Path(__file__).resolve().parents[3] / "docker-compose.yml"
    if not compose.exists():
        pytest.skip("docker-compose.yml not shipped into runtime image")
    text = compose.read_text()
    for name in ("VOICE_URL", "VOICE_STT_URL", "VOICE_BACKEND", "VOICE_API_TOKEN"):
        assert name not in text
