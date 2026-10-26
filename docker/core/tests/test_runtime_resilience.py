"""Faults hit real client boundaries; no provider or database is contacted."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app import circuit_breakers as cb
from app import weather_client
from app.memory import MemoryManager, SessionUnavailable


@pytest.fixture(autouse=True)
def isolated_breakers():
    cb._BREAKERS.clear()
    yield
    cb._BREAKERS.clear()


@pytest.mark.asyncio
async def test_weather_open_breaker_skips_real_http_boundary(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(weather_client, "_get_http", lambda: client)
    cb.get_breaker("weather").force_open()
    assert await weather_client.fetch_weather() is None
    client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_redis_open_breaker_fails_session_closed():
    memory = MemoryManager()
    memory._redis = AsyncMock()
    cb.get_breaker("redis").force_open()
    with pytest.raises(SessionUnavailable):
        await memory.get_session("private-session")
    memory._redis.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_probe_reopens_and_releases_admission():
    breaker = cb.CircuitBreaker(cb.BreakerConfig("cancel", probe_interval_seconds=0))
    breaker.force_open()
    entered = asyncio.Event()

    async def wait():
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(breaker.call(wait))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert breaker.state is cb.State.OPEN
    assert await breaker.call(AsyncMock(return_value=42)) == 42


@pytest.mark.asyncio
async def test_wall_clock_jump_cannot_release_open_breaker(monkeypatch):
    breaker = cb.CircuitBreaker(cb.BreakerConfig("clock", probe_interval_seconds=30))
    breaker.force_open()
    monkeypatch.setattr(cb.time, "time", lambda: 10**15)
    operation = AsyncMock()
    with pytest.raises(cb.CircuitOpen):
        await breaker.call(operation)
    operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_weather_fault_and_recovery_uses_one_half_open_probe(monkeypatch):
    requests = 0

    def response(request):
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"current": {
            "temperature_2m": 12.5, "weather_code": 61, "is_day": 1,
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        monkeypatch.setattr(weather_client, "_get_http", lambda: client)
        breaker = cb.get_breaker("weather")
        breaker.cfg.error_threshold = 1
        assert await weather_client.fetch_weather() is None
        assert breaker.state is cb.State.OPEN
        assert await weather_client.fetch_weather() is None
        assert requests == 1
        breaker.cfg.probe_interval_seconds = 0
        assert (await weather_client.fetch_weather())["condition"] == "rain"
        assert breaker.state is cb.State.CLOSED


@pytest.mark.asyncio
async def test_postgres_open_breaker_denies_auth_without_acquisition(monkeypatch):
    from app import db, auth

    pool = AsyncMock()
    monkeypatch.setattr(db, "_pool", pool)
    cb.get_breaker("postgres").force_open()
    assert await auth.get_user_from_token("revoked-or-unknown") is None
    pool.getconn.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_open_breaker_never_contacts_selected_backend(monkeypatch):
    from app import voice_client

    monkeypatch.setenv("VOICE_BACKEND", "amarillo-xpu")
    client = AsyncMock()
    cb.get_breaker("voice").force_open()
    with pytest.raises(cb.CircuitOpen):
        await voice_client.post_leased_tts(client, {"text": "retained text reply"})
    client.post.assert_not_awaited()
    assert not voice_client._local_gate.locked()


@pytest.mark.asyncio
async def test_mcp_open_breaker_rejects_tool_without_network():
    from app.mcp_client import MCPClient

    client = MCPClient()
    client._http = AsyncMock()
    client._session_id = "existing-session"
    cb.get_breaker("mcp").force_open()
    with pytest.raises(cb.CircuitOpen):
        await client.invoke_tool("search", {"query": "example"})
    client._http.post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dependency",
    ["embedding", "qdrant", "data", "mcp", "anthropic", "lm_studio", "voice"],
)
async def test_transport_failure_recovery_preserves_response_contract(dependency):
    from app.dependencies import http_request

    requests = 0

    def response(request):
        nonlocal requests
        requests += 1
        return httpx.Response(503 if requests == 1 else 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        breaker = cb.get_breaker(dependency)
        breaker.cfg.error_threshold = 1
        first = await http_request(
            dependency, client, "get", "https://test.invalid/resource"
        )
        assert first.status_code == 503
        assert breaker.state is cb.State.OPEN
        with pytest.raises(cb.CircuitOpen):
            await http_request(
                dependency, client, "get", "https://test.invalid/resource"
            )
        assert requests == 1
        breaker.cfg.probe_interval_seconds = 0
        assert (
            await http_request(
                dependency, client, "get", "https://test.invalid/resource"
            )
        ).status_code == 200
        assert breaker.state is cb.State.CLOSED


@pytest.mark.asyncio
async def test_stream_disconnect_is_accounted_and_closes_response():
    from app.dependencies import http_stream

    class BrokenBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadError("disconnect")

        async def aclose(self):
            self.closed = True

    body = BrokenBody()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))
    ) as client:
        breaker = cb.get_breaker("lm_studio")
        breaker.cfg.error_threshold = 1
        with pytest.raises(httpx.ReadError):
            async with http_stream(
                "lm_studio", client, "GET", "https://test.invalid/stream"
            ) as response:
                async for _ in response.aiter_bytes():
                    pass
        assert breaker.state is cb.State.OPEN
    assert body.closed


@pytest.mark.asyncio
async def test_late_probe_success_cannot_override_newer_forced_outage():
    breaker = cb.CircuitBreaker(cb.BreakerConfig("epoch", probe_interval_seconds=0))
    breaker.force_open()
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait():
        entered.set()
        await release.wait()
        return "response"

    task = asyncio.create_task(breaker.call(wait))
    await entered.wait()
    breaker.force_open()
    release.set()
    assert await task == "response"
    assert breaker.state is cb.State.OPEN


@pytest.mark.asyncio
async def test_redis_replacement_and_final_close_release_both_clients(monkeypatch):
    import redis
    from app import memory as memory_module

    memory = MemoryManager()
    original, replacement = AsyncMock(), AsyncMock()
    memory._redis = original

    async def unavailable():
        raise redis.ConnectionError("test outage")

    unavailable.__name__ = "get"
    replacement.get.return_value = "recovered"
    monkeypatch.setattr(
        memory_module.redis, "from_url", lambda *args, **kwargs: replacement
    )
    assert await memory._redis_op(unavailable) == "recovered"
    original.aclose.assert_awaited_once()
    await memory.close()
    replacement.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_pool_connection_obeys_postgres_breaker():
    from app.db import ResilientConnectionPool

    pool = ResilientConnectionPool(
        "postgresql://unused@127.0.0.1:1/unused", min_size=0, max_size=1, open=False
    )
    cb.get_breaker("postgres").force_open()
    with pytest.raises(cb.CircuitOpen):
        async with pool.connection():
            pytest.fail("open dependency granted a connection")
    await pool.close()


@pytest.mark.asyncio
async def test_readiness_requires_session_store_but_qdrant_is_optional(monkeypatch):
    from app.observability import health_cache

    health_cache.clear_cache()
    monkeypatch.setattr(
        health_cache, "_check_database_fresh", AsyncMock(return_value={"status": "ok"})
    )
    monkeypatch.setattr(
        health_cache, "_check_redis_fresh", AsyncMock(return_value={"status": "down"})
    )
    monkeypatch.setattr(
        health_cache, "_check_qdrant_fresh", AsyncMock(return_value={"status": "down"})
    )
    assert (await health_cache.get_fresh_health())["status"] == "unhealthy"
    monkeypatch.setattr(
        health_cache, "_check_redis_fresh", AsyncMock(return_value={"status": "ok"})
    )
    assert (await health_cache.get_fresh_health())["status"] == "degraded"
    health_cache.clear_cache()


@pytest.mark.asyncio
async def test_voice_cancellation_cleanup_bypasses_synthesis_breaker(monkeypatch):
    from app import voice_client
    from unittest.mock import MagicMock

    cleanup = AsyncMock()
    cleanup.post.return_value = httpx.Response(
        200, request=httpx.Request("POST", "https://test.invalid/unload")
    )
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=cleanup)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(voice_client.httpx, "AsyncClient", lambda **kwargs: context)
    cb.get_breaker("voice").force_open()
    await voice_client._unload_local_voice()
    cleanup.post.assert_awaited_once()
    assert cleanup.post.call_args.args[0].endswith("/unload")
    assert cb.get_breaker("voice").state is cb.State.OPEN
