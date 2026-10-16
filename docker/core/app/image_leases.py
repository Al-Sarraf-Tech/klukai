"""Bounded image GPU ownership; cleanup completes before releasing its lease."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
import logging
from typing import TypeVar

from .gpu_lease import GPULease, GPULeaseError

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


async def run_leased(
    render: Callable[[GPULease], Awaitable[_T]],
    *,
    lock: asyncio.Semaphore,
    gate: AbstractAsyncContextManager[object],
    acquire: Callable[[str], AbstractAsyncContextManager[GPULease]],
    timeout: float,
    interrupt: Callable[[GPULease], Awaitable[bool]],
    free: Callable[[GPULease], Awaitable[bool]],
) -> _T | None:
    async with lock:
        async with gate:
            try:
                async with acquire("comfyui") as lease:
                    try:
                        async with asyncio.timeout(timeout):
                            return await render(lease)
                    except TimeoutError:
                        logger.error(
                            "Image generation exceeded the bounded GPU lease window"
                        )
                        interrupted = await interrupt(lease)
                        cleaned = await free(lease)
                        if not interrupted or not cleaned:
                            raise GPULeaseError(
                                "ComfyUI timeout cleanup could not be confirmed"
                            ) from None
                        return None
            except GPULeaseError as exc:
                logger.warning("Image GPU handoff refused: %s", exc)
                return None
