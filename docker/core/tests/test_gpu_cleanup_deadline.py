"""Absolute cleanup deadlines, actual trickled HTTP, and shutdown DB ordering."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from contextlib import suppress

import httpx
import pytest

from app import gpu_lease as leases
from app import main
from .test_application_shutdown import application  # noqa: F401


@pytest.fixture
def short_deadlines(monkeypatch):
    monkeypatch.setattr(
        leases, "GPU_LEASE_RELEASE_DEADLINE_SECONDS", 0.04, raising=False
    )
    monkeypatch.setattr(leases, "GPU_LEASE_EVENT_DEADLINE_SECONDS", 0.02, raising=False)
    monkeypatch.setattr(
        leases, "lm_studio_auth_headers", lambda: {"Authorization": "Bearer test-only"}
    )


@pytest.mark.asyncio
async def test_absolute_release_deadline_stops_a_real_slow_trickled_response(
    short_deadlines, monkeypatch
):
    disconnected = asyncio.Event()
    requests = []

    async def trickle(reader, writer):
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 10000\r\n\r\n")
            while True:
                writer.write(b"x")
                await writer.drain()
                await asyncio.sleep(0.005)  # Always faster than the per-read timeout.
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            disconnected.set()

    server = await asyncio.start_server(trickle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(leases, "_gateway_url", lambda: f"http://127.0.0.1:{port}")
    monkeypatch.setattr(leases, "GPU_LEASE_RELEASE_READ_TIMEOUT_SECONDS", 0.025)
    published = AsyncMock()
    monkeypatch.setattr(leases, "_publish_lease_event", published)
    try:
        async with server, httpx.AsyncClient(trust_env=False) as client:
            async with asyncio.timeout(0.3):
                with pytest.raises(leases.GPULeaseError, match="unconfirmed"):
                    await leases.release_gpu_lease(
                        client, leases.GPULease(600, "test-capability")
                    )
            await asyncio.wait_for(disconnected.wait(), 1)
        assert len(requests) == 1
        published.assert_not_awaited()  # An expired cleanup is never a release receipt.
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_release_deadline_includes_retry_backoff(short_deadlines, monkeypatch):
    attempts = []

    async def unavailable(*args, **kwargs):
        attempts.append(kwargs["json"]["lease_token"])
        return httpx.Response(503)

    monkeypatch.setattr(leases, "http_request", unavailable)
    async with asyncio.timeout(0.3):
        with pytest.raises(leases.GPULeaseError, match="unconfirmed"):
            await leases.release_gpu_lease(
                AsyncMock(), leases.GPULease(600, "same-original-token")
            )
    assert attempts == ["same-original-token"]


@pytest.mark.asyncio
async def test_confirmed_release_is_not_held_open_by_event_publish(
    short_deadlines, monkeypatch
):
    from app import events

    event_finalized = asyncio.Event()

    async def blocked_event(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            event_finalized.set()

    monkeypatch.setattr(
        leases, "http_request", AsyncMock(return_value=httpx.Response(204))
    )
    monkeypatch.setattr(events, "publish", blocked_event)
    async with asyncio.timeout(0.3):
        await leases.release_gpu_lease(
            AsyncMock(), leases.GPULease(600, "test-capability")
        )
    assert event_finalized.is_set()


@pytest.mark.asyncio
async def test_repeated_cancellation_joins_release_finalizer_before_database_close(
    application, short_deadlines, monkeypatch
):
    manager, state, _ = application
    leased = asyncio.Event()
    release_started = asyncio.Event()
    permit_finish = asyncio.Event()
    observed = []
    monkeypatch.setattr(
        leases,
        "acquire_gpu_lease",
        AsyncMock(return_value=leases.GPULease(600, "same-token")),
    )

    async def release(client, lease, *, workload):
        release_started.set()
        try:
            await permit_finish.wait()
        finally:
            await asyncio.sleep(0)
            observed.append(
                (state["database_closed"], lease.token, permit_finish.is_set())
            )

    monkeypatch.setattr(leases, "release_gpu_lease", release)

    async def work():
        async with leases.gpu_lease("test-only", client=AsyncMock()):
            leased.set()
            await asyncio.Event().wait()

    scope = main.lifespan(main.app)
    task = closing = None
    try:
        await scope.__aenter__()
        task = asyncio.create_task(work())
        manager.track_task("owner", task, survive_disconnect=True)
        await leased.wait()
        closing = asyncio.create_task(scope.__aexit__(None, None, None))
        await release_started.wait()
        # A second cancellation must not tear through shielded cleanup.
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not closing.done() and not state["database_closed"]
        permit_finish.set()
        await closing
        assert observed == [(False, "same-token", True)]
        assert task.done() and state["database_closed"]
    finally:
        permit_finish.set()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        elif task is not None:
            await scope.__aexit__(None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["_free_comfyui_vram", "_interrupt_comfyui"])
async def test_comfy_finalizer_has_absolute_deadline_and_never_confirms_partial_body(
    short_deadlines, monkeypatch, operation
):
    from app import image_gen

    monkeypatch.setattr(
        leases, "GPU_WORKLOAD_CLEANUP_DEADLINE_SECONDS", 0.04, raising=False
    )
    requests = []
    finalized = asyncio.Event()

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                while True:
                    await asyncio.sleep(0.005)
                    yield b"x"
            finally:
                finalized.set()

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=SlowBody())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(image_gen, "_http", client)
        async with asyncio.timeout(0.3):
            kwargs = {"settle_s": 0} if operation == "_free_comfyui_vram" else {}
            confirmed = await getattr(image_gen, operation)(
                leases.GPULease(600, "original-token"), **kwargs
            )
    assert confirmed is False and finalized.is_set()
    assert len(requests) == 1
    assert requests[0].headers[leases.GPU_LEASE_HEADER] == "original-token"


@pytest.mark.asyncio
async def test_cancellation_during_acquired_event_still_releases_original_capability(
    short_deadlines, monkeypatch
):
    from app import events

    publishing = asyncio.Event()
    requests = []

    async def request(dep, client, method, url, **kwargs):
        requests.append((url, kwargs["json"]))
        if url.endswith(leases.GPU_LEASE_ACQUIRE_PATH):
            return httpx.Response(
                201,
                json={
                    "status": "acquired",
                    "lease_token": "original-acquired-token",
                    "ttl_seconds": 600,
                },
            )
        return httpx.Response(204)

    async def publish(event, **kwargs):
        if event == "gpu_lease.acquired":
            publishing.set()
            await asyncio.Event().wait()
        return 0

    monkeypatch.setattr(leases, "http_request", request)
    monkeypatch.setattr(events, "publish", publish)

    async def work():
        async with leases.gpu_lease("test-only", client=AsyncMock()):
            pytest.fail("Cancelled acquisition must not admit GPU work")

    task = asyncio.create_task(work())
    await publishing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(requests) == 2
    assert requests[1][0].endswith(leases.GPU_LEASE_RELEASE_PATH)
    assert requests[1][1] == {"lease_token": "original-acquired-token"}
