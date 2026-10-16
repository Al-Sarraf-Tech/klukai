"""Shared HTTP factory and close contract for lazy application clients."""

from __future__ import annotations

import asyncio
from weakref import WeakKeyDictionary

import httpx
from typing import Any

from .runtime_tasks import complete

_closing: WeakKeyDictionary[httpx.AsyncClient, asyncio.Task[None]] = WeakKeyDictionary()


def client(
    current: httpx.AsyncClient | None,
    *,
    timeout: float,
    trust_env: bool | None = None,
    follow_redirects: bool | None = None,
) -> httpx.AsyncClient:
    if current is None or current.is_closed:
        options: dict[str, Any] = {"timeout": timeout}
        if trust_env is not None:
            options["trust_env"] = trust_env
        if follow_redirects is not None:
            options["follow_redirects"] = follow_redirects
        return httpx.AsyncClient(**options)
    return current


async def close(current: httpx.AsyncClient | None) -> None:
    if current is None:
        return
    closing = _closing.get(current)
    if closing is None:
        if current.is_closed:
            return
        closing = asyncio.create_task(current.aclose(), name="lazy-http-close")
        _closing[current] = closing
    try:
        await complete(closing)
    finally:
        if closing.done():
            _closing.pop(current, None)
