"""Bound expensive password work without blocking the async server loop."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from threading import BoundedSemaphore
from typing import TypeVar

import bcrypt

_T = TypeVar("_T")
_WORKERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="password")
_CAPACITY = BoundedSemaphore(8)
MAX_PASSWORD_BYTES = 72


class PasswordWorkBusy(Exception):
    """Password capacity is occupied; the caller can retry later."""


async def _run(operation: Callable[[], _T]) -> _T:
    if not _CAPACITY.acquire(blocking=False):
        raise PasswordWorkBusy()

    def work() -> _T:
        try:
            return operation()
        finally:
            # A cancelled asyncio waiter cannot interrupt running bcrypt.
            # Release admission only when the actual CPU work has finished.
            _CAPACITY.release()

    try:
        future = _WORKERS.submit(work)
    except BaseException:
        _CAPACITY.release()
        raise
    # Do not cancel the physical executor item when its caller disappears.
    # Otherwise cancelled queue entries could release admission immediately
    # and accumulate without bound behind the running workers.
    wrapped = asyncio.wrap_future(future)
    wrapped.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
    return await asyncio.shield(wrapped)


async def verify_password(password: str, encoded_hash: str) -> bool:
    raw = password.encode()
    if len(raw) > MAX_PASSWORD_BYTES:
        return False
    return await _run(lambda: bcrypt.checkpw(raw, encoded_hash.encode()))


async def hash_password(password: str) -> str:
    raw = password.encode()
    if len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError("Password exceeds bcrypt's 72-byte limit")
    return (await _run(lambda: bcrypt.hashpw(raw, bcrypt.gensalt()))).decode()
