"""Persistent readiness clients retain fresh checks, breakers and cleanup.

Readiness probes PostgreSQL and Redis only (cloud-only AI, 2026-10-26: no
vector store). The lifespan owns one pooled Redis probe client.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
import redis.asyncio as redis

from app.circuit_breakers import State, get_breaker
from app.observability import health_cache as health


@pytest.fixture
def probe_clients(monkeypatch):
    mode = {"redis": "ok"}
    entered = asyncio.Event()
    cache = AsyncMock()
    created = []

    async def ping():
        if mode["redis"] == "wait":
            entered.set()
            await asyncio.Event().wait()
        if mode["redis"] == "down":
            raise ConnectionError("disposable Redis failure")
        return True

    cache.ping.side_effect = ping

    def factory(*args, **kwargs):
        created.append(kwargs)
        return cache

    monkeypatch.setattr(redis, "from_url", factory)
    database = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(health, "_check_database_fresh", database)
    health.clear_cache()
    return cache, database, created, mode, entered


@pytest.mark.asyncio
async def test_running_reuses_verified_client_but_every_readiness_check_is_fresh(
    probe_clients,
):
    cache, database, created, _, _ = probe_clients
    async with health.running():
        for _ in range(3):
            result = await health.get_fresh_health()
            assert result["status"] == "ok"
            assert "qdrant" not in result
        assert len(created) == 1
        assert database.await_count == cache.ping.await_count == 3
        assert created[0]["socket_timeout"] == health.PING_TIMEOUT_SECONDS
        assert created[0]["socket_connect_timeout"] == health.PING_TIMEOUT_SECONDS
    cache.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_reused_client_breaker_outage_recovery_and_required_redis_policy(
    probe_clients,
):
    cache, _, created, mode, _ = probe_clients
    breaker = get_breaker("redis")
    breaker.cfg.error_threshold = 1
    async with health.running():
        assert (await health.get_fresh_health())["status"] == "ok"
        mode["redis"] = "down"
        assert (await health.get_fresh_health())["status"] == "unhealthy"
        assert breaker.state is State.OPEN
        mode["redis"] = "ok"
        assert (await health.get_fresh_health())["redis"] == "down"
        breaker.cfg.probe_interval_seconds = 0
        assert (await health.get_fresh_health())["status"] == "ok"
        assert breaker.state is State.CLOSED
        assert len(created) == 1
    cache.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_probe_preserves_client_and_next_fresh_probe_recovers(
    probe_clients,
):
    cache, _, created, mode, entered = probe_clients
    async with health.running():
        mode["redis"] = "wait"
        task = asyncio.create_task(health.get_fresh_health())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        cache.aclose.assert_not_awaited()
        mode["redis"] = "ok"
        assert (await health.get_fresh_health())["status"] == "ok"
        assert len(created) == 1
    cache.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_lifespan_closes_client_exactly_once(probe_clients):
    cache, _, _, _, _ = probe_clients
    entered = asyncio.Event()

    async def serving():
        async with health.running():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(serving())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cache.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_client_initialization_releases_owner_and_can_restart(
    probe_clients, monkeypatch
):
    cache, _, _, _, _ = probe_clients
    monkeypatch.setattr(
        redis,
        "from_url",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("factory unavailable")
        ),
    )
    with pytest.raises(RuntimeError):
        async with health.running():
            pytest.fail("Failed client initialization must not serve")
    monkeypatch.setattr(redis, "from_url", lambda *args, **kwargs: cache)
    async with health.running():
        assert (await health.get_fresh_health())["status"] == "ok"
    cache.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_partial_startup_unwinds_probe_lifespan(probe_clients, monkeypatch):
    from app import caches, main, rate_limit

    cache, _, _, _, _ = probe_clients
    monkeypatch.setattr(
        main, "init_pool", AsyncMock(side_effect=RuntimeError("startup failed"))
    )
    monkeypatch.setattr(main, "close_pool", AsyncMock())
    monkeypatch.setattr(caches, "close", AsyncMock())
    monkeypatch.setattr(rate_limit, "close", AsyncMock())
    with pytest.raises(RuntimeError, match="startup failed"):
        async with main.lifespan(main.app):
            pytest.fail("Partial startup must not accept requests")
    cache.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_redis_cleanup_still_releases_owner(probe_clients):
    cache, _, created, _, _ = probe_clients
    cache.aclose.side_effect = RuntimeError("cleanup failed")
    with pytest.raises(RuntimeError, match="cleanup failed"):
        async with health.running():
            assert (await health.get_fresh_health())["status"] == "ok"
    cache.aclose.side_effect = None
    async with health.running():
        assert (await health.get_fresh_health())["status"] == "ok"
    assert len(created) == 2
    assert cache.aclose.await_count == 2
