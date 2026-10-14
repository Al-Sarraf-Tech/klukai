"""Connection churn and generation ownership under controlled suspension."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.ws_manager import WSManager


class Socket:
    def __init__(self):
        self.accept = AsyncMock()
        self.send_text = AsyncMock()
        self.close = AsyncMock()


@pytest.mark.asyncio
async def test_membership_can_change_while_broadcast_awaits():
    manager = WSManager()
    old, new = Socket(), Socket()
    await manager.connect(old, 'u')
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(_):
        entered.set()
        await release.wait()
    old.send_text.side_effect = blocked
    sending = asyncio.create_task(manager.send('u', {'type': 'token'}))
    await entered.wait()
    await manager.connect(new, 'u')
    release.set()
    await sending
    assert manager.is_connected('u')


@pytest.mark.asyncio
async def test_last_send_failure_awaits_old_tasks_and_preserves_reconnect():
    manager = WSManager()
    old, new = Socket(), Socket()
    await manager.connect(old, 'u')
    running, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def background():
        running.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
    task = asyncio.create_task(background())
    manager.track_task('u', task)
    await running.wait()
    old.send_text.side_effect = RuntimeError('closed')
    sending = asyncio.create_task(manager.send('u', {'type': 'token'}))
    try:
        await asyncio.wait_for(cleaning.wait(), .5)
    except TimeoutError:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    await manager.connect(new, 'u')
    new_task = asyncio.create_task(asyncio.Event().wait())
    manager.track_task('u', new_task)
    release.set()
    await sending
    await manager.disconnect('u', old)  # endpoint finally for the old socket
    assert task.done()
    assert not new_task.done()
    assert manager.is_connected('u')
    await manager.disconnect('u', new)
    assert new_task.done()


@pytest.mark.asyncio
async def test_failed_connected_send_cleans_membership():
    manager = WSManager()
    socket = Socket()
    socket.send_text.side_effect = RuntimeError('closed')
    with pytest.raises(ConnectionError):
        await manager.connect(socket, 'u')
    assert not manager.is_connected('u')


@pytest.mark.asyncio
async def test_slow_socket_times_out_and_self_cleanup_does_not_cancel_sender(monkeypatch):
    monkeypatch.setattr('app.ws_manager.SEND_TIMEOUT_SECONDS', .01)
    manager = WSManager()
    socket = Socket()
    await manager.connect(socket, 'u')
    async def slow(_):
        await asyncio.Event().wait()
    socket.send_text.side_effect = slow
    async def sender():
        await manager.send('u', {'type': 'mood'})
        return 'completed'
    task = asyncio.create_task(sender())
    manager.track_task('u', task)
    assert await asyncio.wait_for(task, 1) == 'completed'
    assert not manager.is_connected('u')
    assert 'u' not in manager._user_tasks


@pytest.mark.asyncio
async def test_background_exception_is_retrieved_and_reported(caplog):
    manager = WSManager()
    callback_ran = asyncio.Event()
    async def failed():
        raise RuntimeError('deliberate background failure')
    task = asyncio.create_task(failed())
    manager.track_task('u', task)
    task.add_done_callback(lambda _: callback_ran.set())
    await callback_ran.wait()
    assert not task._log_traceback
    assert 'deliberate background failure' in caplog.text
