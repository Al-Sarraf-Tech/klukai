"""Persistent readiness clients retain fresh checks, breakers and cleanup."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
import redis.asyncio as redis

from app.circuit_breakers import State, get_breaker
from app.observability import health_cache as health


@pytest.fixture
def probe_clients(monkeypatch):
    actual_client = httpx.AsyncClient
    clients = []
    options = []
    mode = {"http": "ok", "redis": "ok"}
    entered = asyncio.Event()
    cache = AsyncMock()

    async def ping():
        if mode["redis"] == "down":
            raise ConnectionError("disposable Redis failure")
        return True

    cache.ping.side_effect = ping

    async def respond(request):
        if mode["http"] == "wait":
            entered.set()
            await asyncio.Event().wait()
        return httpx.Response(503 if mode["http"] == "down" else 200, json={"ok": True})

    def create(**kwargs):
        options.append(kwargs)
        client = actual_client(transport=httpx.MockTransport(respond), **kwargs)
        client.aclose = AsyncMock(wraps=client.aclose)
        clients.append(client)
        return client

    monkeypatch.setattr(health.httpx, "AsyncClient", create)

    def factory(*args, **kwargs):
        return cache

    monkeypatch.setattr(redis, "from_url", factory)
    database = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(health, "_check_database_fresh", database)
    health.clear_cache()
    return clients, cache, database, options, mode, entered


@pytest.mark.asyncio
async def test_running_reuses_verified_clients_but_every_readiness_check_is_fresh(
    probe_clients,
):
    clients, cache, database, options, _, _ = probe_clients
    async with health.running():
        for _ in range(3):
            assert (await health.get_fresh_health())["status"] == "ok"
        assert len(clients) == 1
        assert clients[0].is_closed is False
        assert database.await_count == cache.ping.await_count == 3
        assert options[0]["timeout"] == health.PING_TIMEOUT_SECONDS
        assert options[0].get("verify", True) is not False
        assert options[0].get("trust_env", True) is True
    assert clients[0].is_closed
    clients[0].aclose.assert_awaited_once()
    cache.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency", ["qdrant", "redis"])
async def test_reused_client_breaker_outage_recovery_and_required_dependency_policy(
    probe_clients, dependency
):
    clients, cache, _, _, mode, _ = probe_clients
    breaker = get_breaker(dependency)
    breaker.cfg.error_threshold = 1
    key = "http" if dependency == "qdrant" else "redis"
    async with health.running():
        assert (await health.get_fresh_health())["status"] == "ok"
        mode[key] = "down"
        failed = await health.get_fresh_health()
        assert failed["status"] == (
            "degraded" if dependency == "qdrant" else "unhealthy"
        )
        assert breaker.state is State.OPEN
        mode[key] = "ok"
        assert (await health.get_fresh_health())[dependency] == "down"
        breaker.cfg.probe_interval_seconds = 0
        assert (await health.get_fresh_health())["status"] == "ok"
        assert breaker.state is State.CLOSED
        assert len(clients) == 1
    cache.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_probe_preserves_client_and_next_fresh_probe_recovers(
    probe_clients,
):
    clients, _, _, _, mode, entered = probe_clients
    async with health.running():
        mode["http"] = "wait"
        task = asyncio.create_task(health.get_fresh_health())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not clients[0].is_closed
        mode["http"] = "ok"
        assert (await health.get_fresh_health())["status"] == "ok"
        assert len(clients) == 1
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_cancelled_lifespan_closes_clients_exactly_once(probe_clients):
    clients, cache, _, _, _, _ = probe_clients
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
    assert clients[0].is_closed
    clients[0].aclose.assert_awaited_once()
    cache.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_client_initialization_closes_http_and_can_restart(
    probe_clients, monkeypatch
):
    clients, cache, _, _, _, _ = probe_clients
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
    assert clients[0].is_closed
    monkeypatch.setattr(redis, "from_url", lambda *args, **kwargs: cache)
    async with health.running():
        assert (await health.get_fresh_health())["status"] == "ok"
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_main_partial_startup_unwinds_probe_lifespan(probe_clients, monkeypatch):
    from app import caches, main, rate_limit

    clients, cache, _, _, _, _ = probe_clients
    monkeypatch.setattr(
        main, "init_pool", AsyncMock(side_effect=RuntimeError("startup failed"))
    )
    monkeypatch.setattr(main, "close_pool", AsyncMock())
    monkeypatch.setattr(caches, "close", AsyncMock())
    monkeypatch.setattr(rate_limit, "close", AsyncMock())
    with pytest.raises(RuntimeError, match="startup failed"):
        async with main.lifespan(main.app):
            pytest.fail("Partial startup must not accept requests")
    assert clients[0].is_closed
    cache.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_redis_cleanup_still_closes_http_and_releases_owner(probe_clients):
    clients, cache, _, _, _, _ = probe_clients
    cache.aclose.side_effect = RuntimeError("cleanup failed")
    with pytest.raises(RuntimeError, match="cleanup failed"):
        async with health.running():
            assert (await health.get_fresh_health())["status"] == "ok"
    assert clients[0].is_closed
    cache.aclose.side_effect = None
    async with health.running():
        assert (await health.get_fresh_health())["status"] == "ok"
    assert len(clients) == 2 and all(client.is_closed for client in clients)
