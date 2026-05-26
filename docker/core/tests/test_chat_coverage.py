"""Behavioral unit tests for app.chat: WebSocket endpoint, voice, tap-interact.

These exercise the WS turn handler (auth gate, session restore, mood
restoration, mission-timer restore, the receive loop dispatch to
message/voice/tap handlers) and the voice STT->LLM->TTS pipeline, all with
the underlying services mocked. Every test asserts observable behavior:
connection accepted/rejected, the right handler invoked per message type,
TTS audio sent, error fallbacks surfaced.

No live WebSocket server or FastAPI runtime is booted — the inner
``websocket_endpoint`` coroutine is captured via a fake app decorator and
driven directly with a fake WebSocket + fake WS manager.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import SessionState  # noqa: E402


# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakeWebSocket:
    def __init__(self, token="tok"):
        self.query_params = {"token": token} if token is not None else {}
        self.close = AsyncMock()


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


def _fake_ws_manager(receive_queue):
    """WS manager whose receive() pops queued client messages then None."""
    ws = MagicMock()
    ws.connect = AsyncMock()
    ws.disconnect = AsyncMock()
    ws.track_task = MagicMock()
    ws.send_mood = AsyncMock()
    ws.send_proactive = AsyncMock()
    ws.send_voice = AsyncMock()
    ws.send_thinking = AsyncMock()
    ws.is_connected = MagicMock(return_value=True)
    q = list(receive_queue)

    async def _receive(user_id="default"):
        if q:
            return q.pop(0)
        return None

    ws.receive = AsyncMock(side_effect=_receive)
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


class TestHandleVoice:
    @pytest.mark.asyncio
    async def test_happy_path_stt_llm_tts(self):
        """Transcript -> _handle_message -> TTS audio sent to client."""
        from app.chat import _handle_voice

        session = SessionState(conversation_id="c1")
        client = _voice_client(stt_text="draw yourself", tts_status=200, tts_bytes=b"wavdata")

        ws = MagicMock()
        ws.send_thinking = AsyncMock()
        ws.send_voice = AsyncMock()
        ws.send_proactive = AsyncMock()

        memory = MagicMock()
        returned = SessionState(
            conversation_id="c1",
            turns=[{"role": "assistant", "content": "Here you go."}],
        )
        memory.get_session = AsyncMock(return_value=returned)
        handle_message = AsyncMock()

        with patch("httpx.AsyncClient", return_value=client), patch(
            "app.chat.ws", ws
        ), patch("app.chat.memory", memory), patch(
            "app.chat._handle_message", handle_message
        ):
            await _handle_voice("base64audio", session, "u1")

        handle_message.assert_awaited_once()
        assert handle_message.await_args.args[0] == "draw yourself"
        ws.send_voice.assert_awaited_once()
        assert ws.send_voice.await_args.kwargs.get("final") is True

    @pytest.mark.asyncio
    async def test_stt_failure_surfaces_garbled_message(self):
        from app.chat import _handle_voice

        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(side_effect=RuntimeError("stt 500"))

        ws = MagicMock()
        ws.send_thinking = AsyncMock()
        ws.send_proactive = AsyncMock()
        handle_message = AsyncMock()

        with patch("httpx.AsyncClient", return_value=client), patch(
            "app.chat.ws", ws
        ), patch("app.chat._handle_message", handle_message):
            await _handle_voice("aud", SessionState(conversation_id="c1"), "u1")

        handle_message.assert_not_called()
        ws.send_proactive.assert_awaited_once()
        assert "garbled" in ws.send_proactive.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_empty_transcript_prompts_retry(self):
        from app.chat import _handle_voice

        client = _voice_client(stt_text="   ")  # whitespace only
        ws = MagicMock()
        ws.send_thinking = AsyncMock()
        ws.send_proactive = AsyncMock()
        handle_message = AsyncMock()

        with patch("httpx.AsyncClient", return_value=client), patch(
            "app.chat.ws", ws
        ), patch("app.chat._handle_message", handle_message):
            await _handle_voice("aud", SessionState(conversation_id="c1"), "u1")

        handle_message.assert_not_called()
        ws.send_proactive.assert_awaited_once()
        assert "heard nothing" in ws.send_proactive.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_tts_http_error_falls_back_to_text(self):
        from app.chat import _handle_voice

        session = SessionState(conversation_id="c1")
        client = _voice_client(stt_text="say hi", tts_status=500)

        ws = MagicMock()
        ws.send_thinking = AsyncMock()
        ws.send_voice = AsyncMock()
        ws.send_proactive = AsyncMock()

        memory = MagicMock()
        memory.get_session = AsyncMock(
            return_value=SessionState(
                conversation_id="c1",
                turns=[{"role": "assistant", "content": "Hi."}],
            )
        )

        with patch("httpx.AsyncClient", return_value=client), patch(
            "app.chat.ws", ws
        ), patch("app.chat.memory", memory), patch(
            "app.chat._handle_message", AsyncMock()
        ):
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

        ws = MagicMock()
        ws.send_thinking = AsyncMock()
        ws.send_voice = AsyncMock()
        ws.send_proactive = AsyncMock()

        memory = MagicMock()
        memory.get_session = AsyncMock(
            return_value=SessionState(
                conversation_id="c1",
                turns=[{"role": "assistant", "content": "Hi."}],
            )
        )

        with patch("httpx.AsyncClient", return_value=client), patch(
            "app.chat.ws", ws
        ), patch("app.chat.memory", memory), patch(
            "app.chat._handle_message", AsyncMock()
        ):
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
        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token=None)  # no token query param
        ws = _fake_ws_manager([])

        with patch("app.chat.ws", ws):
            await endpoint(sock)

        sock.close.assert_awaited_once()
        assert sock.close.await_args.kwargs.get("code") == 4001
        ws.connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_invalid_token(self):
        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token="bad")
        ws = _fake_ws_manager([])

        with patch("app.chat.ws", ws), patch(
            "app.auth.get_user_from_token", AsyncMock(return_value=None)
        ):
            await endpoint(sock)

        sock.close.assert_awaited_once()
        assert sock.close.await_args.kwargs.get("code") == 4001
        ws.connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_new_session_created_with_restored_mood(self):
        """Valid token, no existing session, persistent mood 'tender' in DB:
        a new session is created, mood restored + pushed to the client."""
        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token="good")
        ws = _fake_ws_manager([])  # no messages -> loop body skipped, disconnect

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
    async def test_existing_session_mood_corrected_from_db(self):
        """Existing session with drifted mood gets corrected from persistent DB."""
        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token="good")
        ws = _fake_ws_manager([])

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
        ws = _fake_ws_manager([])

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
        sock = _FakeWebSocket(token="good")
        long_content = "x" * 5000
        ws = _fake_ws_manager([{"type": "message", "content": long_content}])

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
        sock = _FakeWebSocket(token="good")
        ws = _fake_ws_manager([{"type": "voice_end", "audio": "b64aud"}])

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
        sock = _FakeWebSocket(token="good")
        ws = _fake_ws_manager([{"type": "tap_interact"}])

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
        sock = _FakeWebSocket(token="good")
        ws = _fake_ws_manager([{"type": "typing"}])

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
        """If receive() raises WebSocketDisconnect, the loop exits cleanly and
        disconnect still runs in the finally block."""
        from fastapi import WebSocketDisconnect

        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token="good")
        ws = _fake_ws_manager([])
        ws.receive = AsyncMock(side_effect=WebSocketDisconnect(code=1001))

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
        ws = _fake_ws_manager([])

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
