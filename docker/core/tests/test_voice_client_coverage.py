"""Behavioral coverage for the voice / TTS-to-dominus shim.

IMPORTANT — scope note for the reviewer:
The task brief referenced ``app/voice_client.py``. That module does NOT exist
in this repo (nor anywhere in git history across all branches); the only entry
for it is a stale ``omit =`` line in ``.coveragerc``. The actual voice shim —
STT then TTS round-trip to the dominus voice service at ``$VOICE_URL`` (default
http://100.107.121.5:8301 over Tailscale) — lives in ``app.chat._handle_voice``. Per the
task rules I must not create ``app/`` code to invent the module, so this file
tests the REAL shim that ships.

These tests deliberately do NOT duplicate the cases already in
``test_chat_coverage.py`` (happy path / STT failure / empty transcript /
TTS HTTP 500). They cover the remaining voice behaviors with real assertions:

  * the audio bytes returned by the dominus /tts endpoint are base64-encoded
    correctly before being pushed over the WebSocket (the TTS encode contract);
  * when the last turn is NOT an assistant message, no /tts call is made;
  * a catastrophic failure (e.g. httpx client construction blows up) surfaces
    the "voice channel broke" text fallback rather than ghosting the user.

All network + WS + memory are mocked. No real dominus, no sockets.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.chat import _handle_voice
from app.models import SessionState


pytestmark = pytest.mark.usefixtures("mock_voice_gpu_lease")


def _routing_client(stt_text: str, tts_status: int = 200, tts_bytes: bytes = b"audio"):
    """Fake httpx.AsyncClient whose post() routes by URL suffix to /stt or /tts.

    Records every URL it was POSTed to in ``client.posted_urls``.
    """
    stt_resp = MagicMock()
    stt_resp.raise_for_status = MagicMock()
    stt_resp.json = MagicMock(return_value={"text": stt_text})

    tts_resp = MagicMock()
    tts_resp.status_code = tts_status
    tts_resp.content = tts_bytes
    tts_resp.text = "err"

    client = MagicMock()
    client.posted_urls = []

    async def _post(url, **kwargs):
        client.posted_urls.append(url)
        return stt_resp if url.endswith("/stt") else tts_resp

    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(side_effect=_post)
    return client


def _ws() -> MagicMock:
    ws = MagicMock()
    ws.send_thinking = AsyncMock()
    ws.send_voice = AsyncMock()
    ws.send_proactive = AsyncMock()
    return ws


class TestVoiceTTSEncoding:
    @pytest.mark.asyncio
    async def test_dominus_audio_is_base64_encoded_over_ws(self):
        """The raw audio bytes from dominus /tts are base64-encoded and pushed
        with final=True — the decoded payload must equal the original bytes."""
        raw = b"\x00\x01\x02RIFFWAVE\xff\xfe"  # binary, non-utf8
        client = _routing_client("read this aloud", tts_status=200, tts_bytes=raw)
        ws = _ws()

        memory = MagicMock()
        memory.get_session = AsyncMock(return_value=SessionState(
            conversation_id="c1",
            turns=[{"role": "assistant", "content": "Acknowledged, Commander."}],
        ))

        with patch("httpx.AsyncClient", return_value=client), \
             patch("app.chat.ws", ws), \
             patch("app.chat.memory", memory), \
             patch("app.chat._handle_message", new=AsyncMock()):
            await _handle_voice("inbound-audio", SessionState(conversation_id="c1"), "u1")

        ws.send_voice.assert_awaited_once()
        sent_b64 = ws.send_voice.await_args.args[1]
        assert base64.b64decode(sent_b64) == raw
        assert ws.send_voice.await_args.kwargs.get("final") is True
        # Both legs of the round-trip hit the dominus voice service.
        assert any(u.endswith("/stt") for u in client.posted_urls)
        assert any(u.endswith("/tts") for u in client.posted_urls)

    @pytest.mark.asyncio
    async def test_voice_url_env_override_is_used(self):
        """VOICE_URL env var redirects both /stt and /tts to the configured host."""
        client = _routing_client("hello", tts_status=200, tts_bytes=b"x")
        ws = _ws()
        memory = MagicMock()
        memory.get_session = AsyncMock(return_value=SessionState(
            conversation_id="c1",
            turns=[{"role": "assistant", "content": "Hi."}],
        ))

        import os
        with patch("httpx.AsyncClient", return_value=client), \
             patch("app.chat.ws", ws), \
             patch("app.chat.memory", memory), \
             patch("app.chat._handle_message", new=AsyncMock()), \
             patch.dict(os.environ, {"VOICE_URL": "http://dominus.local:8301"}, clear=False):
            await _handle_voice("a", SessionState(conversation_id="c1"), "u1")

        assert all(u.startswith("http://dominus.local:8301") for u in client.posted_urls)


class TestVoiceNoTTSWhenNoAssistantTurn:
    @pytest.mark.asyncio
    async def test_skips_tts_when_last_turn_not_assistant(self):
        """If the refreshed session's last turn is the user's (no assistant
        reply yet), no /tts request is made and no audio is sent."""
        client = _routing_client("a question", tts_status=200, tts_bytes=b"x")
        ws = _ws()
        memory = MagicMock()
        memory.get_session = AsyncMock(return_value=SessionState(
            conversation_id="c1",
            turns=[{"role": "user", "content": "a question"}],  # no assistant turn
        ))

        with patch("httpx.AsyncClient", return_value=client), \
             patch("app.chat.ws", ws), \
             patch("app.chat.memory", memory), \
             patch("app.chat._handle_message", new=AsyncMock()):
            await _handle_voice("a", SessionState(conversation_id="c1"), "u1")

        assert not any(u.endswith("/tts") for u in client.posted_urls)
        ws.send_voice.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_tts_when_refreshed_session_empty(self):
        """A refreshed session with no turns must not attempt TTS."""
        client = _routing_client("hi", tts_status=200, tts_bytes=b"x")
        ws = _ws()
        memory = MagicMock()
        memory.get_session = AsyncMock(return_value=SessionState(
            conversation_id="c1", turns=[],
        ))

        with patch("httpx.AsyncClient", return_value=client), \
             patch("app.chat.ws", ws), \
             patch("app.chat.memory", memory), \
             patch("app.chat._handle_message", new=AsyncMock()):
            await _handle_voice("a", SessionState(conversation_id="c1"), "u1")

        assert not any(u.endswith("/tts") for u in client.posted_urls)
        ws.send_voice.assert_not_called()


class TestVoiceCatastrophicFallback:
    @pytest.mark.asyncio
    async def test_outer_failure_sends_broke_entirely_text(self):
        """If the httpx client itself can't be created, the broad outer handler
        surfaces the 'voice channel broke' text fallback (never ghosts)."""
        ws = _ws()
        with patch("httpx.AsyncClient", side_effect=RuntimeError("no transport")), \
             patch("app.chat.ws", ws), \
             patch("app.chat._handle_message", new=AsyncMock()):
            # Must not raise
            await _handle_voice("a", SessionState(conversation_id="c1"), "u1")

        ws.send_proactive.assert_awaited_once()
        assert "broke entirely" in ws.send_proactive.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_outer_failure_swallows_secondary_ws_error(self):
        """Even if the fallback send_proactive ALSO raises, _handle_voice never
        propagates (the inner try/except pass guard)."""
        ws = _ws()
        ws.send_proactive = AsyncMock(side_effect=RuntimeError("ws gone too"))
        with patch("httpx.AsyncClient", side_effect=RuntimeError("no transport")), \
             patch("app.chat.ws", ws), \
             patch("app.chat._handle_message", new=AsyncMock()):
            await _handle_voice("a", SessionState(conversation_id="c1"), "u1")  # must not raise
        ws.send_proactive.assert_awaited_once()
