"""The owner's voice switch: off means core sends no voice work anywhere."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import voice_client  # noqa: E402


def _request() -> MagicMock:
    req = MagicMock()
    req.headers = {"Authorization": "Bearer good"}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    return req


def _route(path: str, method: str):
    from app.routes import register_routes

    app = FastAPI()
    register_routes(app)
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _no_network():
    return patch("httpx.AsyncClient", side_effect=AssertionError("voice host contacted"))


@pytest.fixture
def voice_off(monkeypatch):
    monkeypatch.setenv("KLUKAI_VOICE_ENABLED", "false")


@pytest.mark.parametrize("value", ["false", "0", "no", "off", " OFF "])
def test_off_values(monkeypatch, value):
    monkeypatch.setenv("KLUKAI_VOICE_ENABLED", value)
    assert voice_client.voice_enabled() is False


@pytest.mark.parametrize("value", [None, "true", "1", "yes", "on"])
def test_on_by_default_in_code(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("KLUKAI_VOICE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("KLUKAI_VOICE_ENABLED", value)
    assert voice_client.voice_enabled() is True


@pytest.mark.asyncio
async def test_synthesis_sends_nothing_when_off(voice_off, monkeypatch):
    monkeypatch.setenv("FISH_API_KEY", "pytest-fish-key")
    with _no_network():
        response = await voice_client.post_tts({"text": "hello"})
    assert response.status_code == 503
    assert response.json() == {"error": "Voice is turned off"}
    # Callers may raise_for_status(); it must raise, not crash on no request.
    with pytest.raises(httpx.HTTPStatusError):
        response.raise_for_status()


@pytest.mark.asyncio
async def test_api_tts_is_refused_when_off(voice_off):
    from app.api_schemas import TTSRequest

    handler = _route("/api/tts", "POST")
    with patch("app.routes._get_user_id", new=AsyncMock(return_value="claude")), _no_network():
        response = await handler(TTSRequest(text="hello"), _request())
    assert response.status_code == 503
    assert b"Voice is turned off" in response.body


@pytest.mark.asyncio
async def test_api_tts_still_requires_a_login_when_off(voice_off):
    from app.api_schemas import TTSRequest

    handler = _route("/api/tts", "POST")
    with patch("app.routes._get_user_id", new=AsyncMock(return_value=None)):
        response = await handler(TTSRequest(text="hello"), _request())
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_api_stt_is_refused_when_off(voice_off):
    from app.api_schemas import STTRequest

    handler = _route("/api/stt", "POST")
    with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="claude")), \
         _no_network():
        response = await handler(STTRequest(audio="AAAA"), _request())
    assert response.status_code == 503
    assert b"Voice is turned off" in response.body


@pytest.mark.asyncio
async def test_spoken_chat_says_voice_is_off(voice_off):
    from app.chat import _handle_voice
    from app.models import SessionState

    ws = MagicMock()
    ws.send = AsyncMock()
    ws.send_thinking = AsyncMock()
    with patch("app.chat.ws", ws), _no_network(), \
         patch("app.chat._handle_message", new=AsyncMock(side_effect=AssertionError("replied"))):
        await _handle_voice("AAAA", SessionState(conversation_id="c1"), "claude")
    ws.send_thinking.assert_not_called()
    frame = ws.send.await_args.args[1]
    assert frame["type"] == "error"
    assert "Voice is off" in frame["message"]


@pytest.mark.asyncio
async def test_voice_letter_falls_back_to_text_when_off(voice_off):
    from app.voice_archive import save_voice_note

    with patch("app.voice_client.post_tts", side_effect=AssertionError("synthesized")):
        assert await save_voice_note("I missed you.", user_id="claude") is None


@pytest.mark.asyncio
async def test_health_reports_voice_disabled_without_probing(voice_off):
    handler = _route("/api/health/subsystems", "GET")
    probed: list[str] = []

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            probed.append(url)
            return httpx.Response(200, json={"data": []}, request=httpx.Request("GET", url))

    redis = MagicMock()
    redis.ping = AsyncMock()
    redis.aclose = AsyncMock()
    with patch("app.routes._get_user_id", new=AsyncMock(return_value="claude")), \
         patch("app.db.check_health", new=AsyncMock(return_value={"status": "ok"})), \
         patch("redis.asyncio.from_url", return_value=redis), \
         patch("app.routes_health._image_subsystems",
               new=AsyncMock(return_value={"images": {"status": "ok"}})), \
         patch("httpx.AsyncClient", _Client):
        result = await handler(_request())
    assert result["subsystems"]["voice"] == {"status": "disabled"}
    assert not [url for url in probed if ":8301" in url]
    assert result["status"] == "ok"
