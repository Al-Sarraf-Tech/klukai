"""Bounded local retry policy, always followed by lease-owned cleanup."""

from __future__ import annotations
from collections.abc import Awaitable, Callable
import logging
from .gpu_lease import GPULease, GPULeaseError

logger = logging.getLogger(__name__)


async def render_with_cleanup(
    render: Callable[[], Awaitable[bytes | None]],
    *,
    retry: bool,
    lease: GPULease,
    interrupt: Callable[[GPULease], Awaitable[bool]],
    free: Callable[[GPULease], Awaitable[bool]],
) -> bytes | None:
    try:
        result = await render()
        if result is None and retry:
            logger.info("Image generation retry — interrupting stale job and retrying")
            if not await interrupt(lease):
                raise GPULeaseError("ComfyUI retry interrupt could not be confirmed")
            result = await render()
        return result
    finally:
        if not await free(lease):
            raise GPULeaseError("ComfyUI VRAM cleanup could not be confirmed")
