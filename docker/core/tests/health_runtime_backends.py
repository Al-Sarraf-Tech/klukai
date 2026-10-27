"""Explicit real-protocol reuse/fault gate; never skipped or unit-collected.

KLUKAI_RUNTIME_BACKENDS=1 KLUKAI_RUNTIME_EVIDENCE=<private-directory>
python -m pytest tests/health_runtime_backends.py -q
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import time

import pytest

from app.circuit_breakers import State, get_breaker
from app.observability import health_cache as health
from tests.runtime_backend_support import (
    ROOT,
    SOURCE_PATHS,
    backends as backends,
    clients as clients,
    evidence,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency", ["postgres", "redis"])
async def test_reused_real_readiness_clients_survive_fault_and_recovery(
    backends, clients, dependency
):
    _, proxies = clients
    proxy = proxies[dependency]
    breaker = get_breaker(dependency)
    events = []
    async with health.running():
        assert (await health.get_fresh_health())["status"] == "ok"
        accepted = {name: value.accepted for name, value in proxies.items()}
        for _ in range(3):
            assert (await health.get_fresh_health())["status"] == "ok"
        # The real persistent Redis protocol connection carries fresh commands.
        assert proxies["redis"].accepted == accepted["redis"]
        events.append(
            {
                "phase": "healthy_reused",
                "state": breaker.state.value,
                "monotonic": time.monotonic(),
            }
        )
        await proxy.cut()
        failed = await health.get_fresh_health()
        # Readiness needs both: no database, or no session store, is unhealthy.
        assert failed["status"] == "unhealthy"
        assert breaker.state is State.OPEN
        events.append(
            {
                "phase": "cut",
                "state": breaker.state.value,
                "monotonic": time.monotonic(),
            }
        )
        proxy.available = True
        breaker.cfg.probe_interval_seconds = 0
        assert (await health.get_fresh_health())["status"] == "ok"
        assert breaker.state is State.CLOSED
        events.append(
            {
                "phase": "recovered",
                "state": breaker.state.value,
                "monotonic": time.monotonic(),
            }
        )
        managed = health._clients
        assert managed is not None
    connections = managed.redis.connection_pool._available_connections
    assert all(not connection.is_connected for connection in connections)
    evidence(backends, dependency, events)
    paths = [
        *SOURCE_PATHS,
        "app/main.py",
        "tests/health_runtime_backends.py",
        "tests/test_health_client_lifecycle.py",
    ]
    (backends.evidence / f"{dependency}-driver-binding.json").write_text(
        json.dumps(
            {
                "driver": str(Path(__file__).relative_to(ROOT)),
                "source_sha256": {
                    path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                    for path in paths
                },
                "managed_redis_connections_closed": True,
            },
            indent=2,
        )
        + "\n"
    )


@pytest.mark.asyncio
async def test_real_readiness_cancellation_releases_stream_and_reuses_client(
    backends, clients, monkeypatch
):
    _, proxies = clients
    proxy = proxies["redis"]
    entered, release = asyncio.Event(), asyncio.Event()
    original = proxy._relay

    async def paused(reader, writer):
        proxy.accepted += 1
        proxy.writers.add(writer)
        try:
            # The first RESP command on the probe's fresh pooled connection.
            request = await reader.readuntil(b"\r\n")
            assert request.startswith(b"*")
            entered.set()
            await release.wait()
        finally:
            writer.close()
            await writer.wait_closed()
            proxy.writers.discard(writer)

    # Prove the actual backend first, then start the cancellation case with a
    # clean transport. Cutting an idle reused socket would inject an additional
    # ConnectionError unrelated to cancellation and legitimately trip it.
    async with health.running():
        assert (await health.get_fresh_health())["status"] == "ok"
    async with health.running():
        managed = health._clients
        monkeypatch.setattr(proxy, "_relay", paused)
        task = asyncio.create_task(health.get_fresh_health())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert health._clients is managed and managed is not None
            # Cancellation is not a dependency failure.
            assert get_breaker("redis").state is State.CLOSED
        finally:
            release.set()
            monkeypatch.setattr(proxy, "_relay", original)
            await asyncio.sleep(0)
        # The same managed client serves the next probe on a fresh stream.
        assert (await health.get_fresh_health())["status"] == "ok"
        assert health._clients is managed
    connections = managed.redis.connection_pool._available_connections
    assert all(not connection.is_connected for connection in connections)
    (backends.evidence / "cancellation-driver-binding.json").write_text(
        json.dumps(
            {
                "driver": "tests/health_runtime_backends.py",
                "driver_sha256": hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
                "health_sha256": hashlib.sha256(
                    (ROOT / "app/observability/health_cache.py").read_bytes()
                ).hexdigest(),
                "cancelled_probe_recovered": True,
                "managed_redis_connections_closed": True,
            },
            indent=2,
        )
        + "\n"
    )
