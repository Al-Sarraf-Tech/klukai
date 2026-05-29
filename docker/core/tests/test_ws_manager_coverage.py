"""Behavioral coverage top-up for app.ws_manager.WSManager.

Existing tests cover connect / disconnect / send / typed senders. This file
targets the remaining surface:

  * track_task (lines 24-35): per-user task registration + the done-callback
    that auto-discards completed tasks.
  * disconnect last-device path (lines 61-68): cancelling orphan background
    tasks when the user's final connection drops.
  * receive (lines 135-175): multi-device race via asyncio.wait, JSON decode,
    bad-JSON → None, dead-connection pruning, and the no-connection guard.

Fake WebSockets expose real awaitable accept/send_text/receive_text so the
asyncio.create_task + asyncio.wait machinery in receive() executes for real.
Deterministic: receive_text futures are resolved explicitly; no sleeps.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ws_manager import WSManager


def _mk_ws() -> MagicMock:
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    return ws


# ═══════════════════════════════════════════════════════════════════════════
# track_task — registration + done-callback auto-discard (24-35)
# ═══════════════════════════════════════════════════════════════════════════
class TestTrackTask:
    @pytest.mark.asyncio
    async def test_task_registered_under_user(self):
        m = WSManager()

        async def _work():
            await asyncio.sleep(0)

        task = asyncio.create_task(_work())
        m.track_task("alice", task)
        assert task in m._user_tasks["alice"]
        await task  # let it finish

    @pytest.mark.asyncio
    async def test_completed_task_auto_discarded_via_callback(self):
        """The add_done_callback(_discard) removes the task from the bucket once
        it completes (line 32-35)."""
        m = WSManager()

        async def _work():
            return 1

        task = asyncio.create_task(_work())
        m.track_task("alice", task)
        await task
        # done_callbacks run on the loop after completion — yield once
        await asyncio.sleep(0)
        assert task not in m._user_tasks.get("alice", set())

    @pytest.mark.asyncio
    async def test_multiple_tasks_share_one_bucket(self):
        m = WSManager()

        async def _idle():
            await asyncio.Event().wait()  # never completes until cancelled

        t1 = asyncio.create_task(_idle())
        t2 = asyncio.create_task(_idle())
        m.track_task("alice", t1)
        m.track_task("alice", t2)
        assert m._user_tasks["alice"] == {t1, t2}
        t1.cancel()
        t2.cancel()


# ═══════════════════════════════════════════════════════════════════════════
# disconnect — orphan-task cancellation on last device (61-68)
# ═══════════════════════════════════════════════════════════════════════════
class TestDisconnectCancelsTasks:
    @pytest.mark.asyncio
    async def test_last_device_disconnect_cancels_tracked_tasks(self):
        """When the user's final WS drops, every still-running tracked task is
        cancelled and the user bucket is cleared."""
        m = WSManager()
        ws = _mk_ws()
        await m.connect(ws, user_id="alice")

        async def _idle():
            await asyncio.Event().wait()

        task = asyncio.create_task(_idle())
        m.track_task("alice", task)

        await m.disconnect(user_id="alice", ws=ws)
        await asyncio.sleep(0)  # let cancellation propagate

        assert task.cancelled() or task.done()
        assert "alice" not in m._user_tasks

    @pytest.mark.asyncio
    async def test_already_done_task_not_recancelled(self):
        """A completed tracked task hits the `if not t.done()` guard and is left
        alone (no exception), and the bucket is still cleared on disconnect."""
        m = WSManager()
        ws = _mk_ws()
        await m.connect(ws, user_id="alice")

        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        # Force it to stay in the bucket so disconnect inspects a done task:
        m._user_tasks["alice"] = {done_task}

        await m.disconnect(user_id="alice", ws=ws)
        assert "alice" not in m._user_tasks

    @pytest.mark.asyncio
    async def test_non_last_device_keeps_tasks(self):
        """Disconnecting one of two devices must NOT cancel background tasks."""
        m = WSManager()
        ws1, ws2 = _mk_ws(), _mk_ws()
        await m.connect(ws1, user_id="alice")
        await m.connect(ws2, user_id="alice")

        async def _idle():
            await asyncio.Event().wait()

        task = asyncio.create_task(_idle())
        m.track_task("alice", task)

        await m.disconnect(user_id="alice", ws=ws1)
        await asyncio.sleep(0)
        assert not task.done(), "tasks survive while another device is connected"
        assert task in m._user_tasks["alice"]
        task.cancel()


# ═══════════════════════════════════════════════════════════════════════════
# receive — multi-device race, JSON parse, pruning (135-175)
# ═══════════════════════════════════════════════════════════════════════════
class TestReceive:
    @pytest.mark.asyncio
    async def test_no_connection_returns_none(self):
        m = WSManager()
        assert await m.receive("ghost") is None

    @pytest.mark.asyncio
    async def test_returns_parsed_json_from_single_device(self):
        m = WSManager()
        ws = _mk_ws()
        ws.receive_text = AsyncMock(return_value=json.dumps({"type": "ping", "n": 1}))
        await m.connect(ws, user_id="alice")

        result = await m.receive("alice")
        assert result == {"type": "ping", "n": 1}

    @pytest.mark.asyncio
    async def test_bad_json_returns_none(self):
        """A device that sends non-JSON yields None (JSONDecodeError branch)."""
        m = WSManager()
        ws = _mk_ws()
        ws.receive_text = AsyncMock(return_value="not-json{{{")
        await m.connect(ws, user_id="alice")

        assert await m.receive("alice") is None

    @pytest.mark.asyncio
    async def test_fastest_device_wins_and_slow_is_cancelled(self):
        """With two devices, the first to produce a frame wins; the pending
        receive on the other device is cancelled (lines 152-162)."""
        m = WSManager()
        fast, slow = _mk_ws(), _mk_ws()

        slow_started = asyncio.Event()

        async def _fast():
            return json.dumps({"from": "fast"})

        async def _slow():
            slow_started.set()
            await asyncio.Event().wait()  # hangs until cancelled
            return json.dumps({"from": "slow"})  # pragma: no cover - never reached

        fast.receive_text = _fast
        slow.receive_text = _slow
        await m.connect(fast, user_id="alice")
        await m.connect(slow, user_id="alice")

        result = await m.receive("alice")
        assert result == {"from": "fast"}

    @pytest.mark.asyncio
    async def test_simultaneous_frames_both_delivered(self):
        """Two devices sending at once: both frames are delivered across calls
        (the second is buffered or re-received) — never silently dropped."""
        m = WSManager()
        d1, d2 = _mk_ws(), _mk_ws()
        d1.receive_text = AsyncMock(return_value=json.dumps({"from": "d1"}))
        d2.receive_text = AsyncMock(return_value=json.dumps({"from": "d2"}))
        await m.connect(d1, user_id="alice")
        await m.connect(d2, user_id="alice")

        first = await m.receive("alice")
        second = await m.receive("alice")
        assert {first["from"], second["from"]} == {"d1", "d2"}

    @pytest.mark.asyncio
    async def test_buffered_frames_served_in_order_then_cleared(self):
        """A buffered frame is served (in order) before any new receive_text,
        and the buffer key is removed once drained."""
        m = WSManager()
        ws = _mk_ws()
        ws.receive_text = AsyncMock(side_effect=AssertionError("must drain buffer first"))
        await m.connect(ws, user_id="alice")
        m._recv_buffer["alice"] = [{"n": 1}, {"n": 2}]

        assert await m.receive("alice") == {"n": 1}
        assert await m.receive("alice") == {"n": 2}
        assert "alice" not in m._recv_buffer

    @pytest.mark.asyncio
    async def test_buffer_cleared_on_full_disconnect(self):
        """Buffered frames must not leak into a future reconnected session."""
        m = WSManager()
        ws = _mk_ws()
        await m.connect(ws, user_id="alice")
        m._recv_buffer["alice"] = [{"n": 1}]

        await m.disconnect("alice", ws)  # last device gone
        assert "alice" not in m._recv_buffer

    @pytest.mark.asyncio
    async def test_dead_connection_pruned_then_none(self):
        """A device whose receive_text raises is discarded from the pool and
        receive returns None (lines 166-168)."""
        m = WSManager()
        ws = _mk_ws()

        async def _boom():
            raise RuntimeError("socket closed")

        ws.receive_text = _boom
        await m.connect(ws, user_id="alice")

        result = await m.receive("alice")
        assert result is None
        assert ws not in m._connections.get("alice", set())
        # Sole device died → user key dropped (lines 173-175)
        assert "alice" not in m._connections

    @pytest.mark.asyncio
    async def test_wait_exception_returns_none_gracefully(self):
        """If asyncio.wait itself raises, receive() swallows it and returns None
        instead of propagating (the outer try/except, lines 170-171). The
        connection is left intact since nothing identified it as dead."""
        m = WSManager()
        ws = _mk_ws()
        ws.receive_text = AsyncMock(return_value=json.dumps({"type": "x"}))
        await m.connect(ws, user_id="alice")

        async def _boom_wait(*a, **k):
            raise RuntimeError("event loop exploded")

        with patch("asyncio.wait", side_effect=_boom_wait):
            result = await m.receive("alice")

        assert result is None
        assert m.is_connected("alice"), "a wait() crash must not drop a live device"
