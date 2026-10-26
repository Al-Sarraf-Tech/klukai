"""The spoken round trip in ``app.chat._handle_voice``, end to end on Fish.

Speech-to-text and TTS both go to the Fish Audio cloud API. Here the real
``voice_client`` code runs against an ``httpx.MockTransport``, so these tests
check the wire: the multipart ASR request, the TTS request that follows, and
the base64 contract of the audio pushed over the WebSocket.

Complements ``test_chat_coverage.py`` (which patches the Fish calls) and
``test_voice_stt_fish.py`` (the ASR client on its own). No network, no WS.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.chat import _handle_voice
from app.models import SessionState

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32
KEY = "pytest-fish-key-never-logged"


@pytest.fixture(autouse=True)
def fish(monkeypatch):
    monkeypatch.setenv("KLUKAI_VOICE_ENABLED", "true")
    monkeypatch.setenv("FISH_API_KEY", KEY)
    monkeypatch.delenv("KLUKAI_FISH_MODEL", raising=False)
    monkeypatch.delenv("KLUKAI_FISH_VOICE_ID", raising=False)


def _fish_transport(transcript: str = "hello", tts_status: int = 200, tts_body: bytes = WAV):
    """Patch httpx.AsyncClient onto a MockTransport playing Fish; returns (patch, seen)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/asr":
            return httpx.Response(200, json={"text": transcript, "duration": 1.2})
        assert request.url.path == "/v1/tts"
        return httpx.Response(tts_status, content=tts_body)

    real = httpx.AsyncClient
    factory = patch(
        "httpx.AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs),
    )
    return factory, seen


def _ws() -> MagicMock:
    ws = MagicMock()
    ws.send = AsyncMock()
    ws.send_thinking = AsyncMock()
    ws.send_voice = AsyncMock()
    ws.send_proactive = AsyncMock()
    return ws


AUDIO_B64 = base64.b64encode(b"\x1aE\xdf\xa3webm-take").decode()


class TestFishRoundTrip:
    @pytest.mark.asyncio
    async def test_asr_then_tts_and_base64_audio_over_ws(self):
        factory, seen = _fish_transport("read this aloud")
        ws = _ws()
        handle = AsyncMock(return_value="Acknowledged, Commander.")

        with factory, patch("app.chat.ws", ws), patch("app.chat._handle_message", handle):
            await _handle_voice(AUDIO_B64, SessionState(conversation_id="c1"), "u1")

        assert [str(r.url) for r in seen] == [
            "https://api.fish.audio/v1/asr",
            "https://api.fish.audio/v1/tts",
        ]
        asr, tts = seen
        assert asr.headers["model"] == "transcribe-1-pro"
        assert asr.headers["authorization"] == f"Bearer {KEY}"
        assert asr.headers["content-type"].startswith("multipart/form-data")
        assert b"\x1aE\xdf\xa3webm-take" in asr.content
        assert json.loads(tts.content)["text"] == "Acknowledged, Commander."
        assert handle.await_args.args[0] == "read this aloud"
        ws.send_voice.assert_awaited_once()
        assert base64.b64decode(ws.send_voice.await_args.args[1]) == WAV
        assert ws.send_voice.await_args.kwargs.get("final") is True

    @pytest.mark.asyncio
    async def test_fish_tts_failure_falls_back_to_text(self):
        factory, _ = _fish_transport("hi", tts_status=402, tts_body=b'{"message":"no credit"}')
        ws = _ws()
        with factory, patch("app.chat.ws", ws), patch(
            "app.chat._handle_message", AsyncMock(return_value="Hi.")
        ):
            await _handle_voice(AUDIO_B64, SessionState(conversation_id="c1"), "u1")
        ws.send_voice.assert_not_called()
        assert "offline" in ws.send_proactive.await_args.args[1].lower()


class TestNoTTSWithoutAReply:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("reply", [None, ""])
    async def test_nothing_of_hers_is_not_voiced(self, reply):
        """No answer (the sentinel turn) or an empty one: Fish TTS is never
        called and no audio is sent."""
        factory, seen = _fish_transport("a question")
        ws = _ws()
        with factory, patch("app.chat.ws", ws), patch(
            "app.chat._handle_message", AsyncMock(return_value=reply)
        ):
            await _handle_voice(AUDIO_B64, SessionState(conversation_id="c1"), "u1")
        assert [r.url.path for r in seen] == ["/v1/asr"]
        ws.send_voice.assert_not_called()


class TestVoiceInputGuards:
    @pytest.mark.asyncio
    async def test_without_a_fish_key_nothing_is_sent(self, monkeypatch):
        monkeypatch.delenv("FISH_API_KEY")
        ws = _ws()
        with patch("httpx.AsyncClient", side_effect=AssertionError("Fish contacted")), \
             patch("app.chat.ws", ws), \
             patch("app.chat._handle_message", AsyncMock(side_effect=AssertionError("replied"))):
            await _handle_voice(AUDIO_B64, SessionState(conversation_id="c1"), "u1")
        ws.send_thinking.assert_not_called()
        frame = ws.send.await_args.args[1]
        assert frame["type"] == "error"
        assert "Voice input is off" in frame["message"]

    @pytest.mark.asyncio
    async def test_the_transcript_is_never_logged(self, caplog):
        caplog.set_level("DEBUG")
        factory, _ = _fish_transport("my private words", tts_status=500, tts_body=b"")
        ws = _ws()
        with factory, patch("app.chat.ws", ws), patch(
            "app.chat._handle_message", AsyncMock(return_value="Noted.")
        ):
            await _handle_voice(AUDIO_B64, SessionState(conversation_id="c1"), "u1")
        assert "my private words" not in caplog.text
        assert KEY not in caplog.text


class TestVoiceCatastrophicFallback:
    @pytest.mark.asyncio
    async def test_outer_failure_sends_broke_entirely_text(self):
        """A failure outside the STT/TTS legs surfaces the 'voice channel
        broke' text fallback (never ghosts)."""
        ws = _ws()
        ws.send_thinking = AsyncMock(side_effect=RuntimeError("socket hiccup"))
        with patch("app.chat.ws", ws), patch("app.chat._handle_message", new=AsyncMock()):
            await _handle_voice(AUDIO_B64, SessionState(conversation_id="c1"), "u1")

        ws.send_proactive.assert_awaited_once()
        assert "broke entirely" in ws.send_proactive.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_outer_failure_swallows_secondary_ws_error(self):
        """Even if the fallback send_proactive ALSO raises, _handle_voice never
        propagates."""
        ws = _ws()
        ws.send_thinking = AsyncMock(side_effect=RuntimeError("socket hiccup"))
        ws.send_proactive = AsyncMock(side_effect=RuntimeError("ws gone too"))
        with patch("app.chat.ws", ws), patch("app.chat._handle_message", new=AsyncMock()):
            await _handle_voice(AUDIO_B64, SessionState(conversation_id="c1"), "u1")
        ws.send_proactive.assert_awaited_once()
