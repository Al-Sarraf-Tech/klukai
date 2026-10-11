"""Behavioral unit tests for app.chat: WebSocket endpoint, voice, tap-interact.

These exercise the WS turn handler (auth gate, session restore, mood
restoration, mission-timer restore, the receive loop dispatch to
message/voice/tap handlers) and the voice STT->LLM->TTS pipeline, all with
the underlying services mocked. Every test asserts observable behavior:
connection accepted/rejected, the right handler invoked per message type,
TTS audio sent, error fallbacks surfaced.

No live WebSocket server or FastAPI runtime is booted — the inner
``websocket_endpoint`` coroutine is captured via a fake app decorator and
driven directly with a fake WebSocket + fake WS manager. The endpoint reads
inbound frames from ITS OWN socket, so the fake socket carries the queued
frames and raises WebSocketDisconnect once they are drained.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import SessionState  # noqa: E402


pytestmark = pytest.mark.usefixtures("mock_voice_gpu_lease")


# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakeWebSocket:
    """One device's socket: queued inbound frames, then a disconnect.

    ``calls`` records the order of accept/close so a test can prove the
    4001 rejection completes the handshake before closing.
    """

    def __init__(self, token="tok", frames=()):
        self.query_params = {"token": token} if token is not None else {}
        self.calls: list[str] = []
        self.accept = AsyncMock(side_effect=lambda: self.calls.append("accept"))
        self.close = AsyncMock(side_effect=lambda **kw: self.calls.append("close"))
        self._frames = [f if isinstance(f, str) else json.dumps(f) for f in frames]
        self.receive_text = AsyncMock(side_effect=self._receive_text)

    async def _receive_text(self):
        if self._frames:
            return self._frames.pop(0)
        raise WebSocketDisconnect(code=1000)


class _FakeConn:
    """Minimal async-context DB conn returning a canned mood row."""

    def __init__(self, mood_row=("composed",)):
        self._mood_row = mood_row

    async def execute(self, sql, params=None):
        res = MagicMock()
        res.fetchone = AsyncMock(return_value=self._mood_row)
        return res

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _fake_ws_manager():
    """Outbound-only WS manager fake — the endpoint reads its own socket."""
    ws = MagicMock()
    ws.connect = AsyncMock()
    ws.disconnect = AsyncMock()
    ws.track_task = MagicMock()
    ws.send = AsyncMock()
    ws.send_mood = AsyncMock()
    ws.send_proactive = AsyncMock()
    ws.send_voice = AsyncMock()
    ws.send_thinking = AsyncMock()
    ws.is_connected = MagicMock(return_value=True)
    return ws


def _capture_endpoint():
    """Register the WS endpoint on a fake app and return the inner coroutine."""
    from app.chat import register_websocket

    captured = {}
    fake_app = MagicMock()

    def _decorator(path):
        def _wrap(fn):
            captured["fn"] = fn
            captured["path"] = path
            return fn

        return _wrap

    fake_app.websocket = _decorator
    register_websocket(fake_app)
    return captured["fn"], captured["path"]


# ── _handle_tap_interact ────────────────────────────────────────────────────────


class TestTapInteract:
    @pytest.mark.asyncio
    async def test_tap_triggers_proactive_when_allowed(self):
        from app.chat import _handle_tap_interact

        proactive = MagicMock()
        proactive._can_send = MagicMock(return_value=True)
        proactive.trigger_tap = AsyncMock()
        ws = MagicMock()
        ws.send_proactive = AsyncMock()

        with patch("app.chat.proactive", proactive), patch("app.chat.ws", ws):
            await _handle_tap_interact("u1")

        proactive.trigger_tap.assert_awaited_once()
        ws.send_proactive.assert_not_called()

    @pytest.mark.asyncio
    async def test_tap_falls_back_to_ack_when_blocked(self):
        from app.chat import _handle_tap_interact

        proactive = MagicMock()
        proactive._can_send = MagicMock(return_value=False)
        proactive.trigger_tap = AsyncMock()
        ws = MagicMock()
        ws.send_proactive = AsyncMock()

        with patch("app.chat.proactive", proactive), patch("app.chat.ws", ws):
            await _handle_tap_interact("u1")

        proactive.trigger_tap.assert_not_called()
        ws.send_proactive.assert_awaited_once()
        assert "Commander" in ws.send_proactive.await_args.args[1]


# ── _handle_voice ────────────────────────────────────────────────────────────────


def _voice_client(stt_text="hello there", tts_status=200, tts_bytes=b"audio"):
    """Build a fake httpx.AsyncClient whose post() routes by URL suffix."""
    stt_resp = MagicMock()
    stt_resp.raise_for_status = MagicMock()
    stt_resp.json = MagicMock(return_value={"text": stt_text})

    tts_resp = MagicMock()
    tts_resp.status_code = tts_status
    tts_resp.content = tts_bytes
    tts_resp.text = "err"

    async def _post(url, **kwargs):
        if url.endswith("/stt"):
            return stt_resp
        return tts_resp

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(side_effect=_post)
    return client


def _voice_ws() -> MagicMock:
    ws = MagicMock()
    ws.send = AsyncMock()
    ws.send_thinking = AsyncMock()
    ws.send_voice = AsyncMock()
    ws.send_proactive = AsyncMock()
    return ws


def _tts_posts(client) -> list:
    """The JSON bodies POSTed to the voice /tts endpoint."""
    return [
        c.kwargs["json"] for c in client.post.call_args_list if c.args[0].endswith("/tts")
    ]


class TestHandleVoice:
    @pytest.mark.asyncio
    async def test_happy_path_stt_llm_tts(self):
        """Transcript -> _handle_message -> her reply is voiced to the client."""
        from app.chat import _handle_voice

        session = SessionState(conversation_id="c1")
        client = _voice_client(stt_text="draw yourself", tts_status=200, tts_bytes=b"wavdata")
        ws = _voice_ws()
        handle_message = AsyncMock(return_value="Here you go.")

        with patch("httpx.AsyncClient", return_value=client), patch(
            "app.chat.ws", ws
        ), patch("app.chat._handle_message", handle_message):
            await _handle_voice("base64audio", session, "u1")

        handle_message.assert_awaited_once()
        assert handle_message.await_args.args[0] == "draw yourself"
        assert _tts_posts(client) == [{"text": "Here you go."}]
        ws.send_voice.assert_awaited_once()
        assert ws.send_voice.await_args.kwargs.get("final") is True

    @pytest.mark.asyncio
    async def test_stt_failure_sends_error_frame_so_client_clears_listening(self):
        """STT failure goes out as an `error` frame (never a proactive toast,
        never persisted) — that is what clears the client's "Listening..."."""
        from app.chat import _handle_voice

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(side_effect=RuntimeError("stt 500"))
        ws = _voice_ws()
        handle_message = AsyncMock()

        with patch("httpx.AsyncClient", return_value=client), patch(
            "app.chat.ws", ws
        ), patch("app.chat._handle_message", handle_message):
            await _handle_voice("aud", SessionState(conversation_id="c1"), "u1")

        handle_message.assert_not_called()
        ws.send.assert_awaited_once()
        frame = ws.send.await_args.args[1]
        assert frame["type"] == "error"
        assert "garbled" in frame["message"].lower()
        ws.send_proactive.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_transcript_sends_error_frame(self):
        from app.chat import _handle_voice

        client = _voice_client(stt_text="   ")  # whitespace only
        ws = _voice_ws()
        handle_message = AsyncMock()

        with patch("httpx.AsyncClient", return_value=client), patch(
            "app.chat.ws", ws
        ), patch("app.chat._handle_message", handle_message):
            await _handle_voice("aud", SessionState(conversation_id="c1"), "u1")

        handle_message.assert_not_called()
        ws.send.assert_awaited_once()
        frame = ws.send.await_args.args[1]
        assert frame["type"] == "error"
        assert "heard nothing" in frame["message"].lower()
        ws.send_proactive.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_tts_when_she_had_no_answer(self):
        """_handle_message returns None when her core produced nothing (the
        sentinel turn): no GPU lease is spent voicing an outage notice, and
        no fallback toast either — the sentinel bubble already says it."""
        from app.chat import _handle_voice

        client = _voice_client(stt_text="are you there", tts_status=200)
        ws = _voice_ws()

        with patch("httpx.AsyncClient", return_value=client), patch(
            "app.chat.ws", ws
        ), patch("app.chat._handle_message", AsyncMock(return_value=None)):
            await _handle_voice("aud", SessionState(conversation_id="c1"), "u1")

        assert _tts_posts(client) == []
        ws.send_voice.assert_not_called()
        ws.send_proactive.assert_not_called()

    @pytest.mark.asyncio
    async def test_tts_text_has_stage_directions_stripped(self):
        from app.chat import _handle_voice

        client = _voice_client(stt_text="status", tts_status=200)
        ws = _voice_ws()
        reply = "(she looks up) *a short nod* Acknowledged, Commander."

        with patch("httpx.AsyncClient", return_value=client), patch(
            "app.chat.ws", ws
        ), patch("app.chat._handle_message", AsyncMock(return_value=reply)):
            await _handle_voice("aud", SessionState(conversation_id="c1"), "u1")

        assert _tts_posts(client) == [{"text": "Acknowledged, Commander."}]
        ws.send_voice.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_tts_when_reply_is_only_stage_directions(self):
        from app.chat import _handle_voice

        client = _voice_client(stt_text="status", tts_status=200)
        ws = _voice_ws()

        with patch("httpx.AsyncClient", return_value=client), patch(
            "app.chat.ws", ws
        ), patch("app.chat._handle_message", AsyncMock(return_value="(she nods) *silence*")):
            await _handle_voice("aud", SessionState(conversation_id="c1"), "u1")

        assert _tts_posts(client) == []
        ws.send_voice.assert_not_called()

    @pytest.mark.asyncio
    async def test_long_reply_is_clipped_at_a_sentence_end(self):
        """The voice path had no cap at all; XTTS now gets one clip's worth,
        cut after a sentence rather than mid-word."""
        from app.chat import _handle_voice

        client = _voice_client(stt_text="tell me everything", tts_status=200)
        ws = _voice_ws()
        reply = "This is one full sentence of hers. " * 40  # 1400 chars

        with patch("httpx.AsyncClient", return_value=client), patch(
            "app.chat.ws", ws
        ), patch("app.chat._handle_message", AsyncMock(return_value=reply)):
            await _handle_voice("aud", SessionState(conversation_id="c1"), "u1")

        [body] = _tts_posts(client)
        assert len(body["text"]) <= 500
        assert body["text"].endswith("sentence of hers.")

    @pytest.mark.asyncio
    async def test_tts_http_error_falls_back_to_text(self):
        from app.chat import _handle_voice

        session = SessionState(conversation_id="c1")
        client = _voice_client(stt_text="say hi", tts_status=500)
        ws = _voice_ws()

        with patch("httpx.AsyncClient", return_value=client), patch(
            "app.chat.ws", ws
        ), patch("app.chat._handle_message", AsyncMock(return_value="Hi.")):
            await _handle_voice("aud", session, "u1")

        ws.send_voice.assert_not_called()
        ws.send_proactive.assert_awaited_once()
        assert "offline" in ws.send_proactive.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_tts_exception_falls_back_to_text(self):
        from app.chat import _handle_voice

        session = SessionState(conversation_id="c1")

        stt_resp = MagicMock()
        stt_resp.raise_for_status = MagicMock()
        stt_resp.json = MagicMock(return_value={"text": "say hi"})

        async def _post(url, **kwargs):
            if url.endswith("/stt"):
                return stt_resp
            raise RuntimeError("tts boom")

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(side_effect=_post)
        ws = _voice_ws()

        with patch("httpx.AsyncClient", return_value=client), patch(
            "app.chat.ws", ws
        ), patch("app.chat._handle_message", AsyncMock(return_value="Hi.")):
            await _handle_voice("aud", session, "u1")

        ws.send_voice.assert_not_called()
        ws.send_proactive.assert_awaited_once()
        assert "dropped" in ws.send_proactive.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_outer_failure_with_failing_fallback_is_swallowed(self):
        """Outer except fires, and even the 'channel broke' fallback raises —
        the nested except swallows it so _handle_voice never propagates."""
        from app.chat import _handle_voice

        ws = MagicMock()
        ws.send_thinking = AsyncMock()
        ws.send_proactive = AsyncMock(side_effect=RuntimeError("ws gone"))

        with patch("httpx.AsyncClient", side_effect=RuntimeError("no client")), patch(
            "app.chat.ws", ws
        ), patch("app.chat._handle_message", AsyncMock()):
            # Must not raise despite both the client and the fallback failing.
            await _handle_voice("aud", SessionState(conversation_id="c1"), "u1")

        ws.send_proactive.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_outer_failure_surfaces_channel_broke(self):
        """An exception before/around the client (here: client construction)
        hits the outer except and sends the 'channel broke' fallback."""
        from app.chat import _handle_voice

        ws = MagicMock()
        ws.send_thinking = AsyncMock()
        ws.send_proactive = AsyncMock()

        with patch("httpx.AsyncClient", side_effect=RuntimeError("no client")), patch(
            "app.chat.ws", ws
        ), patch("app.chat._handle_message", AsyncMock()):
            await _handle_voice("aud", SessionState(conversation_id="c1"), "u1")

        ws.send_proactive.assert_awaited_once()
        assert "broke entirely" in ws.send_proactive.await_args.args[1].lower()


# ── websocket_endpoint ───────────────────────────────────────────────────────────


class TestWebSocketEndpoint:
    @pytest.mark.asyncio
    async def test_path_is_ws(self):
        _fn, path = _capture_endpoint()
        assert path == "/ws"

    @pytest.mark.asyncio
    async def test_rejects_missing_token(self):
        """accept() THEN close(4001): a close before the handshake is an HTTP
        403 under uvicorn and the code never reaches the client, so the app
        would reconnect forever instead of returning to login."""
        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token=None)  # no token query param
        ws = _fake_ws_manager()

        with patch("app.chat.ws", ws):
            await endpoint(sock)

        assert sock.calls == ["accept", "close"]
        assert sock.close.await_args.kwargs.get("code") == 4001
        assert sock.close.await_args.kwargs.get("reason") == "Authentication required"
        ws.connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_invalid_token(self):
        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token="bad")
        ws = _fake_ws_manager()

        with patch("app.chat.ws", ws), patch(
            "app.auth.get_user_from_token", AsyncMock(return_value=None)
        ):
            await endpoint(sock)

        assert sock.calls == ["accept", "close"]
        assert sock.close.await_args.kwargs.get("code") == 4001
        assert sock.close.await_args.kwargs.get("reason") == "Invalid or expired token"
        ws.connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_new_session_created_with_restored_mood(self):
        """Valid token, no existing session, persistent mood 'tender' in DB:
        a new session is created, mood restored + pushed to the client."""
        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token="good")  # no frames -> loop exits on disconnect
        ws = _fake_ws_manager()

        memory = MagicMock()
        memory.get_session = AsyncMock(return_value=None)  # no session yet
        memory.save_session = AsyncMock()

        proactive = MagicMock()
        proactive.mission_active = False

        with patch("app.chat.ws", ws), patch("app.chat.memory", memory), patch(
            "app.chat.proactive", proactive
        ), patch("app.auth.get_user_from_token", AsyncMock(return_value="u1")), patch(
            "app.chat.get_conn", return_value=_FakeConn(("tender",))
        ), patch(
            "app.chat._create_conversation", AsyncMock()
        ) as create_conv, patch(
            "app.chat._maybe_reflect_on_return", AsyncMock()
        ):
            await endpoint(sock)

        ws.connect.assert_awaited_once()
        memory.save_session.assert_awaited()  # new session persisted
        create_conv.assert_awaited_once()
        ws.send_mood.assert_awaited_once_with("u1", "tender")
        ws.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_warms_her_day_with_the_session_mood(self):
        """The connect-time outfit pick carries the mood the chat turn will
        pass, so her mood counts even though this pick wins the race."""
        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token="good")
        ws = _fake_ws_manager()

        memory = MagicMock()
        memory.get_session = AsyncMock(return_value=None)
        memory.save_session = AsyncMock()

        proactive = MagicMock()
        proactive.mission_active = False

        with patch("app.chat.ws", ws), patch("app.chat.memory", memory), patch(
            "app.chat.proactive", proactive
        ), patch("app.auth.get_user_from_token", AsyncMock(return_value="u1")), patch(
            "app.chat.get_conn", return_value=_FakeConn(("tender",))
        ), patch(
            "app.chat._create_conversation", AsyncMock()
        ), patch(
            "app.chat._maybe_reflect_on_return", AsyncMock()
        ), patch(
            "app.wardrobe.warm_today", AsyncMock()
        ) as warm:
            await endpoint(sock)

        warm.assert_called_once_with("u1", mood="tender")

    @pytest.mark.asyncio
    async def test_existing_session_mood_corrected_from_db(self):
        """Existing session with drifted mood gets corrected from persistent DB."""
        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token="good")
        ws = _fake_ws_manager()

        existing = SessionState(conversation_id="c1", mood="composed")
        memory = MagicMock()
        memory.get_session = AsyncMock(return_value=existing)
        memory.save_session = AsyncMock()

        proactive = MagicMock()
        proactive.mission_active = False

        with patch("app.chat.ws", ws), patch("app.chat.memory", memory), patch(
            "app.chat.proactive", proactive
        ), patch("app.auth.get_user_from_token", AsyncMock(return_value="u1")), patch(
            "app.chat.get_conn", return_value=_FakeConn(("longing",))
        ), patch(
            "app.chat._maybe_reflect_on_return", AsyncMock()
        ):
            await endpoint(sock)

        assert existing.mood == "longing"  # corrected in place
        memory.save_session.assert_awaited()
        ws.send_mood.assert_awaited_once_with("u1", "longing")

    @pytest.mark.asyncio
    async def test_mission_timer_restored_from_session(self):
        """A session carrying mission state restarts the proactive mission timer."""
        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token="good")
        ws = _fake_ws_manager()

        existing = SessionState(
            conversation_id="c1",
            mood="composed",
            mission_description="patrol",
            mission_interval=20,
        )
        memory = MagicMock()
        memory.get_session = AsyncMock(return_value=existing)
        memory.save_session = AsyncMock()

        proactive = MagicMock()
        proactive.mission_active = False
        proactive.set_affection_level = MagicMock()
        proactive.start_mission = MagicMock()

        affection = MagicMock()
        aff_state = MagicMock()
        aff_state.level = 6
        affection.get_state = AsyncMock(return_value=aff_state)

        with patch("app.chat.ws", ws), patch("app.chat.memory", memory), patch(
            "app.chat.proactive", proactive
        ), patch("app.chat.affection", affection), patch(
            "app.auth.get_user_from_token", AsyncMock(return_value="u1")
        ), patch(
            "app.chat.get_conn", return_value=_FakeConn(("composed",))
        ), patch(
            "app.chat._maybe_reflect_on_return", AsyncMock()
        ):
            await endpoint(sock)

        proactive.set_affection_level.assert_called_once_with(6)
        proactive.start_mission.assert_called_once_with("patrol", 20)

    @pytest.mark.asyncio
    async def test_message_type_dispatches_to_handle_message(self):
        """A 'message' frame is dispatched (and truncated to 4000 chars)."""
        endpoint, _ = _capture_endpoint()
        long_content = "x" * 5000
        sock = _FakeWebSocket(token="good", frames=[{"type": "message", "content": long_content}])
        ws = _fake_ws_manager()

        existing = SessionState(conversation_id="c1", mood="composed")
        memory = MagicMock()
        memory.get_session = AsyncMock(return_value=existing)
        memory.save_session = AsyncMock()

        proactive = MagicMock()
        proactive.mission_active = False
        handle_message = AsyncMock()

        with patch("app.chat.ws", ws), patch("app.chat.memory", memory), patch(
            "app.chat.proactive", proactive
        ), patch("app.auth.get_user_from_token", AsyncMock(return_value="u1")), patch(
            "app.chat.get_conn", return_value=_FakeConn(("composed",))
        ), patch(
            "app.chat._maybe_reflect_on_return", AsyncMock()
        ), patch(
            "app.chat._handle_message", handle_message
        ):
            await endpoint(sock)

        handle_message.assert_awaited_once()
        # Truncated to the 4000-char input limit.
        assert len(handle_message.await_args.args[0]) == 4000

    @pytest.mark.asyncio
    async def test_voice_end_dispatches_to_handle_voice(self):
        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token="good", frames=[{"type": "voice_end", "audio": "b64aud"}])
        ws = _fake_ws_manager()

        memory = MagicMock()
        memory.get_session = AsyncMock(
            return_value=SessionState(conversation_id="c1", mood="composed")
        )
        memory.save_session = AsyncMock()
        proactive = MagicMock()
        proactive.mission_active = False
        handle_voice = AsyncMock()

        with patch("app.chat.ws", ws), patch("app.chat.memory", memory), patch(
            "app.chat.proactive", proactive
        ), patch("app.auth.get_user_from_token", AsyncMock(return_value="u1")), patch(
            "app.chat.get_conn", return_value=_FakeConn(("composed",))
        ), patch(
            "app.chat._maybe_reflect_on_return", AsyncMock()
        ), patch(
            "app.chat._handle_voice", handle_voice
        ):
            await endpoint(sock)

        handle_voice.assert_awaited_once()
        assert handle_voice.await_args.args[0] == "b64aud"

    @pytest.mark.asyncio
    async def test_tap_interact_dispatches(self):
        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token="good", frames=[{"type": "tap_interact"}])
        ws = _fake_ws_manager()

        memory = MagicMock()
        memory.get_session = AsyncMock(
            return_value=SessionState(conversation_id="c1", mood="composed")
        )
        memory.save_session = AsyncMock()
        proactive = MagicMock()
        proactive.mission_active = False
        tap = AsyncMock()

        with patch("app.chat.ws", ws), patch("app.chat.memory", memory), patch(
            "app.chat.proactive", proactive
        ), patch("app.auth.get_user_from_token", AsyncMock(return_value="u1")), patch(
            "app.chat.get_conn", return_value=_FakeConn(("composed",))
        ), patch(
            "app.chat._maybe_reflect_on_return", AsyncMock()
        ), patch(
            "app.chat._handle_tap_interact", tap
        ):
            await endpoint(sock)

        tap.assert_awaited_once_with("u1")

    @pytest.mark.asyncio
    async def test_typing_frame_is_noop(self):
        """A 'typing' frame is accepted but dispatches nothing."""
        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token="good", frames=[{"type": "typing"}])
        ws = _fake_ws_manager()

        memory = MagicMock()
        memory.get_session = AsyncMock(
            return_value=SessionState(conversation_id="c1", mood="composed")
        )
        memory.save_session = AsyncMock()
        proactive = MagicMock()
        proactive.mission_active = False
        handle_message = AsyncMock()

        with patch("app.chat.ws", ws), patch("app.chat.memory", memory), patch(
            "app.chat.proactive", proactive
        ), patch("app.auth.get_user_from_token", AsyncMock(return_value="u1")), patch(
            "app.chat.get_conn", return_value=_FakeConn(("composed",))
        ), patch(
            "app.chat._maybe_reflect_on_return", AsyncMock()
        ), patch(
            "app.chat._handle_message", handle_message
        ):
            await endpoint(sock)

        handle_message.assert_not_called()
        ws.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_websocket_disconnect_is_handled(self):
        """If the socket's receive_text raises WebSocketDisconnect, the loop
        exits cleanly and disconnect still runs in the finally block."""
        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token="good")
        sock.receive_text = AsyncMock(side_effect=WebSocketDisconnect(code=1001))
        ws = _fake_ws_manager()

        memory = MagicMock()
        memory.get_session = AsyncMock(
            return_value=SessionState(conversation_id="c1", mood="composed")
        )
        memory.save_session = AsyncMock()
        proactive = MagicMock()
        proactive.mission_active = False

        with patch("app.chat.ws", ws), patch("app.chat.memory", memory), patch(
            "app.chat.proactive", proactive
        ), patch("app.auth.get_user_from_token", AsyncMock(return_value="u1")), patch(
            "app.chat.get_conn", return_value=_FakeConn(("composed",))
        ), patch(
            "app.chat._maybe_reflect_on_return", AsyncMock()
        ):
            # Must not propagate the disconnect.
            await endpoint(sock)

        ws.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_persistent_mood_db_error_defaults_composed(self):
        """A DB error reading persistent mood is swallowed -> defaults to
        'composed' (so no send_mood, since composed is the default)."""
        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token="good")
        ws = _fake_ws_manager()

        memory = MagicMock()
        memory.get_session = AsyncMock(
            return_value=SessionState(conversation_id="c1", mood="composed")
        )
        memory.save_session = AsyncMock()
        proactive = MagicMock()
        proactive.mission_active = False

        def _broken_conn():
            raise RuntimeError("db down")

        with patch("app.chat.ws", ws), patch("app.chat.memory", memory), patch(
            "app.chat.proactive", proactive
        ), patch("app.auth.get_user_from_token", AsyncMock(return_value="u1")), patch(
            "app.chat.get_conn", side_effect=_broken_conn
        ), patch(
            "app.chat._maybe_reflect_on_return", AsyncMock()
        ):
            await endpoint(sock)

        # composed default -> no mood push to client.
        ws.send_mood.assert_not_called()
        ws.disconnect.assert_awaited_once()


# ── WS loop: generic handler crash must not kill the socket (2026-06-11) ─────


class TestWSLoopErrorHandling:
    @pytest.mark.asyncio
    async def test_handler_crash_sends_error_and_keeps_socket(self):
        """A generic _handle_message crash must not tear down the WebSocket:
        an error event is sent to the client and the receive loop continues
        (the user message was already persisted before streaming)."""
        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token="good", frames=[
            {"type": "message", "content": "first"},
            {"type": "message", "content": "second"},
        ])
        ws = _fake_ws_manager()

        memory = MagicMock()
        memory.get_session = AsyncMock(
            return_value=SessionState(conversation_id="c1", mood="composed")
        )
        memory.save_session = AsyncMock()
        proactive = MagicMock()
        proactive.mission_active = False
        handle_message = AsyncMock(side_effect=[RuntimeError("LLM blew up"), None])

        with patch("app.chat.ws", ws), patch("app.chat.memory", memory), patch(
            "app.chat.proactive", proactive
        ), patch("app.auth.get_user_from_token", AsyncMock(return_value="u1")), patch(
            "app.chat.get_conn", return_value=_FakeConn(("composed",))
        ), patch(
            "app.chat._maybe_reflect_on_return", AsyncMock()
        ), patch(
            "app.chat._handle_message", handle_message
        ):
            # Must not propagate the crash.
            await endpoint(sock)

        # Loop survived the crash and processed the second message.
        assert handle_message.await_count == 2
        types = [c.args[1].get("type") for c in ws.send.call_args_list]
        assert "error" in types
        ws.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect_during_handling_still_exits_cleanly(self):
        """WebSocketDisconnect raised inside _handle_message is NOT converted
        into an error event — the loop exits via the normal disconnect path."""
        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token="good", frames=[{"type": "message", "content": "hi"}])
        ws = _fake_ws_manager()

        memory = MagicMock()
        memory.get_session = AsyncMock(
            return_value=SessionState(conversation_id="c1", mood="composed")
        )
        memory.save_session = AsyncMock()
        proactive = MagicMock()
        proactive.mission_active = False
        handle_message = AsyncMock(side_effect=WebSocketDisconnect(code=1001))

        with patch("app.chat.ws", ws), patch("app.chat.memory", memory), patch(
            "app.chat.proactive", proactive
        ), patch("app.auth.get_user_from_token", AsyncMock(return_value="u1")), patch(
            "app.chat.get_conn", return_value=_FakeConn(("composed",))
        ), patch(
            "app.chat._maybe_reflect_on_return", AsyncMock()
        ), patch(
            "app.chat._handle_message", handle_message
        ):
            await endpoint(sock)

        ws.send.assert_not_called()  # no error event for a plain disconnect
        ws.disconnect.assert_awaited_once()


# ── WS loop: each endpoint reads only its own socket ─────────────────────────


class TestWSLoopOwnSocket:
    """The loop used to fan in across every socket of the user, so device A's
    frames — and its disconnect — could be consumed by device B's coroutine
    and tear B down. Each endpoint now reads its own socket and handles the
    JSON decoding that WSManager.receive used to do."""

    def _run(self, sock, **handlers):
        endpoint, _ = _capture_endpoint()
        ws = _fake_ws_manager()
        memory = MagicMock()
        memory.get_session = AsyncMock(
            return_value=SessionState(conversation_id="c1", mood="composed")
        )
        memory.save_session = AsyncMock()
        proactive = MagicMock()
        proactive.mission_active = False
        patches = [
            patch("app.chat.ws", ws), patch("app.chat.memory", memory),
            patch("app.chat.proactive", proactive),
            patch("app.auth.get_user_from_token", AsyncMock(return_value="u1")),
            patch("app.chat.get_conn", return_value=_FakeConn(("composed",))),
            patch("app.chat._maybe_reflect_on_return", AsyncMock()),
        ] + [patch(f"app.chat.{name}", mock) for name, mock in handlers.items()]
        return endpoint, ws, patches

    @pytest.mark.asyncio
    async def test_malformed_frame_is_skipped_and_loop_continues(self):
        sock = _FakeWebSocket(token="good", frames=[
            "{not valid json",
            {"type": "message", "content": "after the bad frame"},
        ])
        handle_message = AsyncMock()
        endpoint, ws, patches = self._run(sock, _handle_message=handle_message)
        for p in patches:
            p.start()
        try:
            await endpoint(sock)
        finally:
            for p in patches:
                p.stop()

        handle_message.assert_awaited_once()
        assert handle_message.await_args.args[0] == "after the bad frame"
        ws.send.assert_not_called()  # a bad frame is not an application error
        ws.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_object_json_is_skipped(self):
        sock = _FakeWebSocket(token="good", frames=['["a", "b"]', "42", {"type": "tap_interact"}])
        tap = AsyncMock()
        endpoint, ws, patches = self._run(sock, _handle_tap_interact=tap)
        for p in patches:
            p.start()
        try:
            await endpoint(sock)
        finally:
            for p in patches:
                p.stop()

        tap.assert_awaited_once_with("u1")
        # Every frame came off THIS socket (3 frames + the disconnect).
        assert sock.receive_text.await_count == 4
