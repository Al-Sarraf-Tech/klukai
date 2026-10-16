"""Authenticated exclusive-GPU leases for non-LLM work on Dominus.

The compatibility gateway owns the cross-process exclusion boundary.  A core
``asyncio.Lock`` can serialize Klukai's own requests, but it cannot stop another
Tailscale client from starting llama.cpp while ComfyUI is rendering.  The lease
API closes that race: acquire waits for current inference, unloads llama.cpp,
and blocks new inference/model loads until release or a short server deadline.

Lease tokens are capabilities.  They are deliberately excluded from repr and
logs and are sent only in the authenticated release body or the
``X-GPU-Lease-Token`` header used by the protected workload facades.
"""

from __future__ import annotations

from .dependencies import http_request
from .runtime_tasks import complete
from typing import Any


import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import os
from typing import AsyncIterator
from collections.abc import Awaitable

import httpx

from .lm_gateway import lm_studio_auth_headers


# This is a fixed safety contract, not a deployment tuning knob.  The image
# path finishes or interrupts its work before this server-side backstop.
GPU_LEASE_TTL_SECONDS = 10 * 60
GPU_LEASE_ACQUIRE_PATH = "/api/v1/gpu/lease/acquire"
GPU_LEASE_RELEASE_PATH = "/api/v1/gpu/lease/release"
GPU_LEASE_HEADER = "X-GPU-Lease-Token"
GPU_LEASE_RELEASE_ATTEMPTS = 3
# Pauses between release attempts.  The gateway coordinates workload cleanup
# for up to ~10s before it can answer a release; retries 0.25s apart all
# landed inside that window and saw the same 5xx, so the lease was left to
# the expiry cleaner.  4s then 8s outlasts the coordination wait.
GPU_LEASE_RELEASE_RETRY_BACKOFF_SECONDS = (4.0, 8.0)
GPU_LEASE_CONNECT_TIMEOUT_SECONDS = 10.0
# Gateway acquisition can include two workload cleanups, llama.cpp quiescence,
# and the native-vLLM acknowledgement. Release likewise waits for positive
# ComfyUI and XTTS unload confirmation. Keep the HTTP read deadlines above
# those bounded server operations so a healthy cleanup is not mistaken for a
# transport failure and retried while the first request still owns the lock.
GPU_LEASE_ACQUIRE_READ_TIMEOUT_SECONDS = 150.0
GPU_LEASE_RELEASE_READ_TIMEOUT_SECONDS = 90.0
# Absolute wall-clock ceilings include slow response bodies and retry backoff.
# A deadline is an unconfirmed release; the gateway retains its safety markers.
GPU_LEASE_RELEASE_DEADLINE_SECONDS = 372.0
GPU_LEASE_EVENT_DEADLINE_SECONDS = 1.0
GPU_WORKLOAD_CLEANUP_DEADLINE_SECONDS = 65.0


class GPULeaseError(RuntimeError):
    """The gateway could not establish or release a safe GPU lease."""


@dataclass(frozen=True, slots=True)
class GPULease:
    """An opaque, bounded lease returned by the Dominus gateway."""

    ttl_seconds: int
    token: str = field(repr=False)


def gpu_lease_auth_headers(lease: GPULease) -> dict[str, str]:
    """Build headers for a gateway resource protected by ``lease``.

    The capability must only be sent to the authenticated gateway facade.  In
    particular, callers must not put it in a URL, payload, exception, or log.
    """
    return {
        **lm_studio_auth_headers(),
        GPU_LEASE_HEADER: lease.token,
    }


def gpu_lease_capability_headers(lease: GPULease) -> dict[str, str]:
    """Build the capability-only header for a lease-aware workload service."""

    return {GPU_LEASE_HEADER: lease.token}


def _gateway_url() -> str:
    return os.environ.get("LM_STUDIO_URL", "http://100.107.121.5:1234").rstrip("/")


def _status_error(action: str, response: httpx.Response) -> GPULeaseError:
    # Do not include response bodies: a proxy or future gateway version could
    # echo credentials or the opaque lease capability in an error document.
    return GPULeaseError(f"GPU lease {action} failed with HTTP {response.status_code}")


def _request_timeout(read_seconds: float) -> httpx.Timeout:
    return httpx.Timeout(
        connect=GPU_LEASE_CONNECT_TIMEOUT_SECONDS,
        read=read_seconds,
        write=GPU_LEASE_CONNECT_TIMEOUT_SECONDS,
        pool=GPU_LEASE_CONNECT_TIMEOUT_SECONDS,
    )


async def _publish_lease_event(action: str, workload: str, **extra: Any) -> None:
    """Best-effort fan-out of GPU lease transitions onto companion Redis events.

    companion-events-bridge republishes them onto homelab.events as
    host.<host>.gpu_lease.<workload>.<action> for rabbitmq-metrics.
    Never raises.
    """
    try:
        from . import events

        async with asyncio.timeout(GPU_LEASE_EVENT_DEADLINE_SECONDS):
            await events.publish(
                f"gpu_lease.{action}",
                data=workload,
                workload=workload,
                domain="gpu_lease",
                action=action,
                **extra,
            )
    except Exception:
        pass


async def acquire_gpu_lease(
    client: httpx.AsyncClient,
    *,
    workload: str,
) -> GPULease:
    """Acquire the fixed-duration exclusive lease from the local LM gateway."""
    try:
        headers = lm_studio_auth_headers()
        response = await http_request(
            "gpu_gateway",
            client,
            "post",
            f"{_gateway_url()}{GPU_LEASE_ACQUIRE_PATH}",
            headers=headers,
            json={
                "owner": "klukai-core",
                "workload": workload,
                "ttl_seconds": GPU_LEASE_TTL_SECONDS,
            },
            timeout=_request_timeout(GPU_LEASE_ACQUIRE_READ_TIMEOUT_SECONDS),
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        raise GPULeaseError("GPU lease acquire was unavailable") from exc
    if response.status_code not in {200, 201}:
        raise _status_error("acquire", response)

    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        raise GPULeaseError("GPU lease acquire returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise GPULeaseError("GPU lease acquire returned an invalid document")

    token = payload.get("lease_token")
    ttl = payload.get("ttl_seconds")
    status = payload.get("status")
    if status != "acquired" or not isinstance(token, str) or not token.strip():
        raise GPULeaseError("GPU lease acquire response was incomplete")
    # A shorter lease could expire while a still-valid Comfy job is running;
    # a longer one violates the agreed server ceiling.  Fail closed on drift.
    if (
        not isinstance(ttl, int)
        or isinstance(ttl, bool)
        or ttl != GPU_LEASE_TTL_SECONDS
    ):
        raise GPULeaseError("GPU lease TTL did not match the fixed safety contract")
    lease = GPULease(token=token, ttl_seconds=ttl)
    try:
        await _publish_lease_event("acquired", workload)
    except asyncio.CancelledError:
        # The capability is known but has not transferred to the context yet.
        # Retain ownership until its release attempt settles on cancellation.
        await complete(
            asyncio.create_task(release_gpu_lease(client, lease, workload=workload))
        )
        raise
    return lease


async def _release_gpu_lease(
    client: httpx.AsyncClient,
    lease: GPULease,
    *,
    workload: str = "unknown",
) -> None:
    """Release an acquired lease without ever placing its token in a URL."""
    last_error: GPULeaseError | None = None
    for attempt in range(GPU_LEASE_RELEASE_ATTEMPTS):
        try:
            headers = lm_studio_auth_headers()
            response = await http_request(
                "gpu_gateway",
                client,
                "post",
                f"{_gateway_url()}{GPU_LEASE_RELEASE_PATH}",
                headers=headers,
                json={"lease_token": lease.token},
                timeout=_request_timeout(GPU_LEASE_RELEASE_READ_TIMEOUT_SECONDS),
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            last_error = GPULeaseError("GPU lease release was unavailable")
            last_error.__cause__ = exc
        else:
            if response.status_code in {200, 204}:
                return
            last_error = _status_error("release", response)
            if response.status_code < 500:
                raise last_error
        if attempt + 1 < GPU_LEASE_RELEASE_ATTEMPTS:
            backoff = GPU_LEASE_RELEASE_RETRY_BACKOFF_SECONDS
            await asyncio.sleep(backoff[min(attempt, len(backoff) - 1)])
    assert last_error is not None
    raise last_error


async def confirm_gpu_cleanup(operation: Awaitable[bool]) -> bool:
    """A partial/slow cleanup response never confirms that GPU work was reaped."""
    try:
        async with asyncio.timeout(GPU_WORKLOAD_CLEANUP_DEADLINE_SECONDS):
            return await operation
    except TimeoutError:
        return False


async def release_gpu_lease(
    client: httpx.AsyncClient,
    lease: GPULease,
    *,
    workload: str = "unknown",
) -> None:
    """Bound the entire cleanup, retaining uncertainty on an expired release."""
    try:
        async with asyncio.timeout(GPU_LEASE_RELEASE_DEADLINE_SECONDS):
            await _release_gpu_lease(client, lease, workload=workload)
    except TimeoutError as exc:
        raise GPULeaseError(
            "GPU lease release deadline expired; release unconfirmed"
        ) from exc
    await _publish_lease_event("released", workload)


@asynccontextmanager
async def gpu_lease(
    workload: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[GPULease]:
    """Acquire and always attempt to release a bounded gateway GPU lease.

    ``client`` is injectable for deterministic tests.  Production callers use
    a short-lived client so lease credentials never become default headers on
    an unrelated HTTP session.
    """
    owned_client = client is None
    # Ignore ambient proxy variables: the literal tailnet route must remain a
    # direct Tailscale connection, and lease calls must never follow redirects.
    http = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
    lease: GPULease | None = None
    try:
        lease = await acquire_gpu_lease(http, workload=workload)
        yield lease
    finally:
        try:
            if lease is not None:
                release_task = asyncio.create_task(
                    release_gpu_lease(http, lease, workload=workload)
                )
                await complete(release_task)
        finally:
            if owned_client:
                await http.aclose()
