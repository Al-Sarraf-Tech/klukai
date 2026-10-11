"""Behavioral coverage top-up for app.ws_manager.WSManager.

Existing tests cover connect / disconnect / send / typed senders. This file
targets the remaining surface:

  * track_task: per-user task registration + the done-callback that
    auto-discards completed tasks.
  * disconnect last-device path: cancelling orphan background tasks when the
    user's final connection drops.

Inbound frames are no longer read through the manager: each endpoint reads
its own socket (see TestWSLoopOwnSocket in test_chat_coverage.py), so the
former cross-socket ``receive`` fan-in and its buffer have no tests here.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

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

    def test_manager_has_no_cross_socket_receive(self):
        """Inbound reads belong to each endpoint's own socket. A shared
        receive() let one device's disconnect be consumed by — and tear
        down — another device's loop, so it must not come back."""
        assert not hasattr(WSManager, "receive")
        assert not hasattr(WSManager(), "_recv_buffer")
