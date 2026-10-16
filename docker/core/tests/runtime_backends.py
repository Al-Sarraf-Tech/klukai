"""Explicit real-backend chaos gate; missing opt-in or Docker fails closed.

Run KLUKAI_RUNTIME_BACKENDS=1 KLUKAI_RUNTIME_EVIDENCE=<directory>
python -m pytest tests/runtime_backends.py -q. The supporting fixtures launch
only pinned disposable PostgreSQL, Redis-compatible Valkey and Qdrant.
"""

from __future__ import annotations
import asyncio
import time
from unittest.mock import AsyncMock
import pytest
from app import auth, db, memory as memory_module
from app.circuit_breakers import State, get_breaker
from app.memory import SessionUnavailable
from app.models import SessionState
from app.observability import health_cache
from tests.runtime_backend_support import (
    backends as backends,
    clients as clients,
    evidence,
)


@pytest.mark.asyncio
async def test_postgres_auth_failure_and_real_protocol_recovery(backends, clients):
    _, proxies = clients
    proxy = proxies["postgres"]
    breaker = get_breaker("postgres")
    events = [
        {
            "phase": "healthy",
            "state": breaker.state.value,
            "monotonic": time.monotonic(),
        }
    ]
    assert (await health_cache.get_fresh_health())["status"] == "ok"
    await proxy.cut()
    assert (await db.check_health())["status"] == "error"
    assert breaker.state is State.OPEN
    events.append(
        {"phase": "cut", "state": breaker.state.value, "monotonic": time.monotonic()}
    )
    assert await auth.get_user_from_token("not-a-production-token") is None
    assert (await health_cache.get_fresh_health())["status"] == "unhealthy"
    proxy.available = True
    breaker.cfg.probe_interval_seconds = 0
    deadline = time.monotonic() + 10
    while (await db.check_health())["status"] != "ok":
        assert time.monotonic() < deadline
        await asyncio.sleep(0.1)
    assert breaker.state is State.CLOSED
    assert (await health_cache.get_fresh_health())["status"] == "ok"
    events.append(
        {
            "phase": "recovered",
            "state": breaker.state.value,
            "monotonic": time.monotonic(),
        }
    )
    evidence(backends, "postgres", events)


@pytest.mark.asyncio
async def test_redis_session_failure_closed_and_preserved_owner_recovery(
    backends, clients
):
    memory, proxies = clients
    breaker = get_breaker("redis")
    state = SessionState(conversation_id="runtime-private-owner")
    await memory.save_session("runtime-private-owner", state)
    assert (
        await memory.get_session("runtime-private-owner")
    ).conversation_id == "runtime-private-owner"
    events = [
        {
            "phase": "healthy",
            "state": breaker.state.value,
            "monotonic": time.monotonic(),
        }
    ]
    await proxies["redis"].cut()
    with pytest.raises(SessionUnavailable):
        await memory.get_session("runtime-private-owner")
    assert breaker.state is State.OPEN
    events.append(
        {"phase": "cut", "state": breaker.state.value, "monotonic": time.monotonic()}
    )
    count = proxies["redis"].accepted
    with pytest.raises(SessionUnavailable):
        await memory.get_session("runtime-private-owner")
    assert proxies["redis"].accepted == count
    assert (await health_cache.get_fresh_health())["status"] == "unhealthy"
    proxies["redis"].available = True
    breaker.cfg.probe_interval_seconds = 0
    restored = await memory.get_session("runtime-private-owner")
    assert restored.conversation_id == state.conversation_id
    assert breaker.state is State.CLOSED
    assert (await health_cache.get_fresh_health())["status"] == "ok"
    events.append(
        {
            "phase": "recovered",
            "state": breaker.state.value,
            "monotonic": time.monotonic(),
        }
    )
    evidence(backends, "redis", events)


@pytest.mark.asyncio
async def test_qdrant_real_search_loss_keeps_optional_memory_and_recovers(
    backends, clients, monkeypatch
):
    memory, proxies = clients
    from app.dependencies import http_request

    breaker = get_breaker("qdrant")
    await memory._ensure_qdrant_collection()
    vector = [1.0] + [0.0] * 767
    monkeypatch.setattr(memory, "embed_text", AsyncMock(return_value=vector))
    response = await http_request(
        "qdrant",
        memory._http,
        "put",
        f"{memory_module.QDRANT_URL}/collections/{memory_module.COLLECTION_NAME}/points?wait=true",
        json={
            "points": [
                {
                    "id": 1,
                    "vector": vector,
                    "payload": {
                        "summary": "private test memory",
                        "user_id": "runtime-owner",
                    },
                }
            ]
        },
    )
    response.raise_for_status()
    assert (await memory.recall_episodes("recall", user_id="runtime-owner"))[0][
        "summary"
    ] == "private test memory"
    assert await memory.recall_episodes("recall", user_id="other-owner") == []
    events = [
        {
            "phase": "healthy",
            "state": breaker.state.value,
            "monotonic": time.monotonic(),
        }
    ]
    await proxies["qdrant"].cut()
    assert await memory.recall_episodes("recall", user_id="runtime-owner") == []
    assert breaker.state is State.OPEN
    count = proxies["qdrant"].accepted
    assert await memory.recall_episodes("recall", user_id="runtime-owner") == []
    assert proxies["qdrant"].accepted == count
    assert (await health_cache.get_fresh_health())["status"] == "degraded"
    events.append(
        {"phase": "cut", "state": breaker.state.value, "monotonic": time.monotonic()}
    )
    proxies["qdrant"].available = True
    breaker.cfg.probe_interval_seconds = 0
    assert (await memory.recall_episodes("recall", user_id="runtime-owner"))[0][
        "summary"
    ] == "private test memory"
    assert breaker.state is State.CLOSED
    assert (await health_cache.get_fresh_health())["status"] == "ok"
    events.append(
        {
            "phase": "recovered",
            "state": breaker.state.value,
            "monotonic": time.monotonic(),
        }
    )
    evidence(backends, "qdrant", events)


@pytest.mark.asyncio
async def test_real_query_cancellation_releases_pool(backends, clients):
    entered = asyncio.Event()

    async def slow_query():
        async with db.get_conn() as connection:
            entered.set()
            await connection.execute("SELECT pg_sleep(30)")

    task = asyncio.create_task(slow_query())
    await entered.wait()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert (await db.check_health())["status"] == "ok"
    assert get_breaker("postgres").state is State.CLOSED
    assert db.get_pool().get_stats().get("requests_waiting", 0) == 0
    evidence(
        backends,
        "cancellation",
        [{"phase": "cancelled", "pool_reusable": True, "monotonic": time.monotonic()}],
    )
