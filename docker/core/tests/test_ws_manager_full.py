"""Tests for app.ws_manager — WSManager multi-device connection pool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ws_manager import WSManager


def _mk_ws(send_raises: bool = False) -> MagicMock:
    """Build a mock WebSocket with accept + send_text + close."""
    ws = MagicMock()
    ws.accept = AsyncMock()
    if send_raises:
        ws.send_text = AsyncMock(side_effect=RuntimeError("connection closed"))
    else:
        ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    return ws


class TestWSManagerInit:
    def test_starts_empty(self):
        m = WSManager()
        assert m._connections == {}
        assert m.connected is False

    def test_is_connected_default_user_false(self):
        m = WSManager()
        assert m.is_connected("default") is False
        assert m.is_connected("alice") is False


class TestConnect:
    @pytest.mark.asyncio
    async def test_first_connect_calls_accept(self):
        m = WSManager()
        ws = _mk_ws()
        await m.connect(ws, user_id="alice")
        ws.accept.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_first_connect_sends_connected_message(self):
        m = WSManager()
        ws = _mk_ws()
        await m.connect(ws, user_id="alice")
        ws.send_text.assert_awaited_once()
        # Payload includes "connected"
        sent = ws.send_text.call_args.args[0]
        assert '"connected"' in sent

    @pytest.mark.asyncio
    async def test_multiple_devices_same_user(self):
        m = WSManager()
        ws1, ws2 = _mk_ws(), _mk_ws()
        await m.connect(ws1, user_id="alice")
        await m.connect(ws2, user_id="alice")
        assert len(m._connections["alice"]) == 2

    @pytest.mark.asyncio
    async def test_different_users_isolated(self):
        m = WSManager()
        ws_a, ws_b = _mk_ws(), _mk_ws()
        await m.connect(ws_a, user_id="alice")
        await m.connect(ws_b, user_id="bob")
        assert len(m._connections["alice"]) == 1
        assert len(m._connections["bob"]) == 1


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_removes_ws(self):
        m = WSManager()
        ws = _mk_ws()
        await m.connect(ws, user_id="alice")
        await m.disconnect(user_id="alice", ws=ws)
        assert m.is_connected("alice") is False

    @pytest.mark.asyncio
    async def test_disconnect_unknown_user_noop(self):
        m = WSManager()
        # Should not raise
        await m.disconnect(user_id="nonexistent")

    @pytest.mark.asyncio
    async def test_disconnect_one_of_multiple_leaves_others(self):
        m = WSManager()
        ws1, ws2 = _mk_ws(), _mk_ws()
        await m.connect(ws1, user_id="alice")
        await m.connect(ws2, user_id="alice")
        await m.disconnect(user_id="alice", ws=ws1)
        # Bob's ws should still be there
        assert len(m._connections.get("alice", set())) == 1


class TestSend:
    @pytest.mark.asyncio
    async def test_no_connection_silent(self):
        m = WSManager()
        # No exception when sending to no one
        await m.send("alice", {"type": "noop"})

    @pytest.mark.asyncio
    async def test_broadcasts_to_all_devices(self):
        m = WSManager()
        ws1, ws2 = _mk_ws(), _mk_ws()
        await m.connect(ws1, user_id="alice")
        await m.connect(ws2, user_id="alice")
        ws1.send_text.reset_mock()
        ws2.send_text.reset_mock()
        await m.send("alice", {"type": "hello"})
        ws1.send_text.assert_awaited_once()
        ws2.send_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dead_connections_pruned(self):
        m = WSManager()
        ws_good = _mk_ws()
        ws_bad = _mk_ws(send_raises=True)
        await m.connect(ws_good, user_id="alice")
        await m.connect(ws_bad, user_id="alice")
        await m.send("alice", {"type": "x"})
        # Bad ws pruned
        assert ws_bad not in m._connections["alice"]
        assert ws_good in m._connections["alice"]

    @pytest.mark.asyncio
    async def test_user_dropped_when_all_dead(self):
        m = WSManager()
        ws_bad = _mk_ws(send_raises=True)
        await m.connect(ws_bad, user_id="alice")
        await m.send("alice", {"type": "x"})
        # User key removed when no live connections remain
        assert "alice" not in m._connections


class TestTypedSenders:
    @pytest.mark.asyncio
    async def test_send_token(self):
        m = WSManager()
        ws = _mk_ws()
        await m.connect(ws, user_id="alice")
        ws.send_text.reset_mock()
        await m.send_token("alice", "hello world")
        sent = ws.send_text.call_args.args[0]
        assert '"token"' in sent
        assert "hello world" in sent

    @pytest.mark.asyncio
    async def test_send_done_with_text(self):
        m = WSManager()
        ws = _mk_ws()
        await m.connect(ws, user_id="alice")
        ws.send_text.reset_mock()
        await m.send_done("alice", "msg-123", "dolphin", final_text="final")
        sent = ws.send_text.call_args.args[0]
        assert '"done"' in sent
        assert "msg-123" in sent
        assert "dolphin" in sent

    @pytest.mark.asyncio
    async def test_send_done_without_text(self):
        m = WSManager()
        ws = _mk_ws()
        await m.connect(ws, user_id="alice")
        ws.send_text.reset_mock()
        await m.send_done("alice", "msg-1", "model-x")
        sent = ws.send_text.call_args.args[0]
        assert "msg-1" in sent

    @pytest.mark.asyncio
    async def test_send_thinking(self):
        m = WSManager()
        ws = _mk_ws()
        await m.connect(ws, user_id="alice")
        ws.send_text.reset_mock()
        await m.send_thinking("alice", "thinking...")
        assert '"thinking"' in ws.send_text.call_args.args[0]

    @pytest.mark.asyncio
    async def test_send_tool_use(self):
        m = WSManager()
        ws = _mk_ws()
        await m.connect(ws, user_id="alice")
        ws.send_text.reset_mock()
        await m.send_tool_use("alice", "recall_memory", "running")
        sent = ws.send_text.call_args.args[0]
        assert "recall_memory" in sent
        assert "running" in sent

    @pytest.mark.asyncio
    async def test_send_mood(self):
        m = WSManager()
        ws = _mk_ws()
        await m.connect(ws, user_id="alice")
        ws.send_text.reset_mock()
        await m.send_mood("alice", "tender")
        assert "tender" in ws.send_text.call_args.args[0]

    @pytest.mark.asyncio
    async def test_send_proactive(self):
        m = WSManager()
        ws = _mk_ws()
        await m.connect(ws, user_id="alice")
        ws.send_text.reset_mock()
        await m.send_proactive("alice", "from Klukai")
        sent = ws.send_text.call_args.args[0]
        assert '"proactive"' in sent

    @pytest.mark.asyncio
    async def test_send_voice(self):
        m = WSManager()
        ws = _mk_ws()
        await m.connect(ws, user_id="alice")
        ws.send_text.reset_mock()
        await m.send_voice("alice", "AABBCC==", final=True)
        sent = ws.send_text.call_args.args[0]
        assert "AABBCC==" in sent
        assert "true" in sent

    @pytest.mark.asyncio
    async def test_send_affection(self):
        m = WSManager()
        ws = _mk_ws()
        await m.connect(ws, user_id="alice")
        ws.send_text.reset_mock()
        await m.send_affection("alice", 500, 5, "Trusted", 10)
        sent = ws.send_text.call_args.args[0]
        assert "Trusted" in sent
        assert "500" in sent

    @pytest.mark.asyncio
    async def test_send_affection_level_change(self):
        m = WSManager()
        ws = _mk_ws()
        await m.connect(ws, user_id="alice")
        ws.send_text.reset_mock()
        await m.send_affection_level_change("alice", 6, "Bonded", "up")
        sent = ws.send_text.call_args.args[0]
        assert "Bonded" in sent
        assert '"up"' in sent

    @pytest.mark.asyncio
    async def test_send_heartbeat_spike(self):
        m = WSManager()
        ws = _mk_ws()
        await m.connect(ws, user_id="alice")
        ws.send_text.reset_mock()
        await m.send_heartbeat_spike("alice", 140, "passionate")
        sent = ws.send_text.call_args.args[0]
        assert "140" in sent
        assert "passionate" in sent


class TestConnectedProperty:
    def test_property_false_when_empty(self):
        m = WSManager()
        assert m.connected is False

    @pytest.mark.asyncio
    async def test_property_true_with_at_least_one(self):
        m = WSManager()
        await m.connect(_mk_ws(), user_id="alice")
        assert m.connected is True
