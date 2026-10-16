"""Fail-closed ComfyUI and XTTS cleanup before GPU marker removal."""

from __future__ import annotations

import asyncio
import time
import httpx
from .gpu_lease import Lease, LeaseStore, LeaseStateError
from .limits import Limits


def _comfy_queue_is_empty(payload: object) -> bool | None:
    if not isinstance(payload, dict):
        return None
    running = payload.get("queue_running")
    pending = payload.get("queue_pending")
    if not isinstance(running, list) or not isinstance(pending, list):
        return None
    return not running and not pending


async def _cleanup_comfyui(limits: Limits, client: httpx.AsyncClient) -> bool:
    """Interrupt, clear, drain, and unload ComfyUI before releasing the GPU."""

    try:
        interrupted = await client.post(
            "/interrupt", timeout=limits.COMFY_CLEANUP_REQUEST_SECONDS
        )
        if interrupted.status_code != 200:
            return False
        cleared = await client.post(
            "/queue",
            json={"clear": True},
            timeout=limits.COMFY_CLEANUP_REQUEST_SECONDS,
        )
        if cleared.status_code != 200:
            return False

        deadline = time.monotonic() + limits.COMFY_QUEUE_DRAIN_SECONDS
        while True:
            queue = await client.get(
                "/queue", timeout=limits.COMFY_CLEANUP_REQUEST_SECONDS
            )
            if queue.status_code != 200:
                return False
            try:
                empty = _comfy_queue_is_empty(queue.json())
            except (TypeError, ValueError):
                return False
            if empty is None:
                return False
            if empty:
                break
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(limits.COMFY_QUEUE_POLL_SECONDS)

        freed = await client.post(
            "/free",
            json={"unload_models": True, "free_memory": True},
            timeout=limits.COMFY_CLEANUP_REQUEST_SECONDS,
        )
        if freed.status_code != 200:
            return False
        confirmation = await client.get(
            "/queue", timeout=limits.COMFY_CLEANUP_REQUEST_SECONDS
        )
        if confirmation.status_code != 200:
            return False
        try:
            return _comfy_queue_is_empty(confirmation.json()) is True
        except (TypeError, ValueError):
            return False
    except Exception:
        return False


async def _cleanup_companion_voice(limits: Limits, client: httpx.AsyncClient) -> bool:
    """Unload XTTS and confirm it is non-resident before lease removal."""

    try:
        response = await client.post(
            "/unload", timeout=limits.VOICE_CLEANUP_REQUEST_SECONDS
        )
        if response.status_code != 200:
            return False
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return False
        if (
            not isinstance(payload, dict)
            or payload.get("status") != "ok"
            or payload.get("tts_loaded") is not False
        ):
            return False
        health = await client.get(
            "/health", timeout=limits.COMFY_CLEANUP_REQUEST_SECONDS
        )
        if health.status_code != 200:
            return False
        try:
            health_payload = health.json()
        except (TypeError, ValueError):
            return False
        return (
            isinstance(health_payload, dict)
            and health_payload.get("tts_ready") is False
        )
    except Exception:
        return False


async def _cleanup_all_gpu_workloads(
    limits: Limits,
    comfyui: httpx.AsyncClient,
    companion_voice: httpx.AsyncClient,
) -> bool:
    """Prove both non-LLM GPU services are empty without short-circuiting."""

    try:
        async with asyncio.timeout(limits.GPU_WORKLOAD_CLEANUP_SECONDS):
            comfy_clean = await _cleanup_comfyui(limits, comfyui)
            voice_clean = await _cleanup_companion_voice(limits, companion_voice)
            return comfy_clean and voice_clean
    except TimeoutError:
        return False


async def _cleanup_and_complete_lease(
    limits: Limits,
    lease_store: LeaseStore,
    lease: Lease,
    comfyui: httpx.AsyncClient,
    companion_voice: httpx.AsyncClient,
) -> bool:
    """Keep the marker fail-closed until its workload cleaner proves success."""

    try:
        lease_store.mark_state(lease.lease_id, "cleaning")
    except (LeaseStateError, OSError):
        return False

    try:
        cleaned = await _cleanup_all_gpu_workloads(limits, comfyui, companion_voice)
    except Exception:
        cleaned = False
    if cleaned:
        try:
            lease_store.complete_cleanup(lease.lease_id)
            return True
        except (LeaseStateError, OSError):
            return False

    try:
        lease_store.mark_state(lease.lease_id, "cleanup_failed")
    except (LeaseStateError, OSError):
        pass
    return False
