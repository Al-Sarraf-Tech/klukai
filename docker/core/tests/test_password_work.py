"""Authentication CPU and queued work stay bounded, including cancellation."""

import asyncio
import threading

import bcrypt
import pytest


async def test_password_roundtrip_and_utf8_byte_limit():
    from app.passwords import hash_password, verify_password

    hashed = await hash_password("correct-password")
    assert await verify_password("correct-password", hashed)
    assert not await verify_password("wrong-password", hashed)
    assert not await verify_password("é" * 37, hashed)
    with pytest.raises(ValueError):
        await hash_password("é" * 37)


async def test_cpu_queue_is_bounded_and_cancelled_running_work_keeps_its_slot(
    monkeypatch,
):
    from app.passwords import PasswordWorkBusy, verify_password

    release = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0

    def blocked(_password, _hash):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            assert release.wait(3), "test failed to release its worker"
            return True
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(bcrypt, "checkpw", blocked)
    tasks = [
        asyncio.create_task(verify_password("fixture", "fixture-hash"))
        for _ in range(20)
    ]
    try:
        for _ in range(100):
            await asyncio.sleep(0.001)
            if active == 2 and sum(t.done() for t in tasks) >= 12:
                break
        assert peak == 2
        rejected = [
            t for t in tasks if t.done() and isinstance(t.exception(), PasswordWorkBusy)
        ]
        assert len(rejected) >= 12
        # Cancelling callers must not permit more CPU work while their
        # already-running bcrypt operations still occupy the two workers.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert active == 2
        # Even cancelled queued calls retain their physical executor slots
        # until drained: cancellation floods cannot grow an unbounded queue.
        with pytest.raises(PasswordWorkBusy):
            await asyncio.wait_for(verify_password("fixture", "fixture-hash"), 0.05)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        for _ in range(100):
            if active == 0:
                break
            await asyncio.sleep(0.001)
    for _ in range(100):
        try:
            assert await verify_password("fixture", "fixture-hash")
            break
        except PasswordWorkBusy:
            await asyncio.sleep(0.001)
    else:
        pytest.fail("capacity was not released after real worker completion")
    assert peak == 2
