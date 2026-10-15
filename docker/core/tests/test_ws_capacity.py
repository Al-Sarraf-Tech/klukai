"""Socket admission stays bounded across concurrent handshakes and failure."""

import asyncio

import pytest

from app.ws_manager import WSManager


class Socket:
    def __init__(self, gate=None, fail=False):
        self.gate, self.fail = gate, fail
        self.entered = asyncio.Event()
        self.code = None

    async def accept(self):
        self.entered.set()
        if self.fail:
            raise ConnectionError("handshake failed")
        if self.gate:
            await self.gate.wait()

    async def send_text(self, _text):
        pass

    async def close(self, code=1000, **_kw):
        self.code = code


async def test_per_user_limit_preserves_existing_devices_and_other_users():
    manager = WSManager(max_connections=3, max_per_user=1)
    first, rejected, other = Socket(), Socket(), Socket()
    await manager.connect(first, "alice")
    with pytest.raises(ConnectionError):
        await manager.connect(rejected, "alice")
    assert rejected.code == 1013
    assert manager.has_connection("alice", first)
    await manager.connect(other, "bob")
    await manager.disconnect("alice", first)
    await manager.connect(Socket(), "alice")


async def test_concurrent_handshakes_cannot_overbook_global_capacity():
    manager = WSManager(max_connections=2, max_per_user=2)
    gate = asyncio.Event()
    first, second, third = Socket(gate), Socket(gate), Socket()
    tasks = [
        asyncio.create_task(manager.connect(s, u))
        for s, u in [(first, "alice"), (second, "bob")]
    ]
    try:
        await asyncio.wait_for(
            asyncio.gather(first.entered.wait(), second.entered.wait()), 1
        )
        with pytest.raises(ConnectionError):
            await manager.connect(third, "charlie")
        assert third.code == 1013
        assert not third.entered.is_set()
        gate.set()
        await asyncio.gather(*tasks)
        assert manager.has_connection("alice", first)
        assert manager.has_connection("bob", second)
    finally:
        gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_handshake_releases_capacity(cancel):
    manager = WSManager(max_connections=1, max_per_user=1)
    socket = Socket(asyncio.Event(), fail=not cancel)
    task = asyncio.create_task(manager.connect(socket, "alice"))
    await asyncio.wait_for(socket.entered.wait(), 1)
    if cancel:
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not manager.is_connected("alice")
    await manager.connect(Socket(), "bob")


async def test_cancelled_initial_delivery_does_not_leave_a_registered_device():
    manager = WSManager(max_connections=1, max_per_user=1)
    socket = Socket()
    sending = asyncio.Event()

    async def blocked(_text):
        sending.set()
        await asyncio.Event().wait()

    socket.send_text = blocked
    task = asyncio.create_task(manager.connect(socket, "alice"))
    await asyncio.wait_for(sending.wait(), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not manager.is_connected("alice")
    await manager.connect(Socket(), "bob")


async def test_stalled_per_user_rejections_count_against_the_global_limit():
    manager = WSManager(max_connections=2, max_per_user=1)
    first = Socket()
    await manager.connect(first, "alice")
    blocked = Socket()
    closing, release = asyncio.Event(), asyncio.Event()

    async def close(**_kwargs):
        closing.set()
        await release.wait()

    blocked.close = close
    task = asyncio.create_task(manager.connect(blocked, "alice"))
    try:
        await asyncio.wait_for(closing.wait(), 1)
        overflow = [Socket() for _ in range(12)]
        await asyncio.gather(
            *(manager.connect(s, "alice") for s in overflow), return_exceptions=True
        )
        assert not any(s.entered.is_set() for s in overflow)
        assert manager.has_connection("alice", first)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
