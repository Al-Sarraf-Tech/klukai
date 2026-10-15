"""Failed startup and shutdown must release every already-owned resource."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import main


@pytest.fixture
def services(monkeypatch):
    for name in (
        "init_pool",
        "close_pool",
        "run_migration",
        "events_init",
        "events_close",
    ):
        monkeypatch.setattr(main, name, AsyncMock())
    resources = {}
    for name in ("memory", "router", "mcp", "affection"):
        obj = MagicMock(init=AsyncMock(), close=AsyncMock())
        resources[name] = obj
        monkeypatch.setattr(main, name, obj)
    monkeypatch.setattr(main, "proactive", MagicMock(close=AsyncMock()))
    monkeypatch.setattr(main, "load_personality", MagicMock())
    monkeypatch.setattr("app.auth.init_users", AsyncMock())
    monkeypatch.setattr("app.auth.cleanup_expired_sessions", AsyncMock())
    monkeypatch.setattr("app.caches.close", AsyncMock())
    monkeypatch.setattr("app.rate_limit.close", AsyncMock())
    app = SimpleNamespace(
        state=SimpleNamespace(voice_jobs=SimpleNamespace(close=AsyncMock()))
    )

    @asynccontextmanager
    async def handoff():
        yield

    monkeypatch.setattr("app.job_handoff.running", handoff)
    return app, resources


@pytest.mark.parametrize(
    "failure",
    [
        "pool",
        "migration",
        "memory",
        "router",
        "mcp",
        "affection",
        "events",
        "personality",
    ],
)
async def test_partial_startup_closes_attempted_resources(
    services, monkeypatch, failure
):
    app, resources = services
    if failure in resources:
        resources[failure].init.side_effect = RuntimeError("startup failed")
    else:
        name = {
            "pool": "init_pool",
            "migration": "run_migration",
            "events": "events_init",
            "personality": "load_personality",
        }[failure]
        getattr(main, name).side_effect = RuntimeError("startup failed")
    with pytest.raises(RuntimeError, match="startup failed"):
        async with main.lifespan(app):
            pytest.fail("startup must not serve")
    main.close_pool.assert_awaited_once()
    for resource in resources.values():
        if resource.init.await_count:
            resource.close.assert_awaited_once()


async def test_cleanup_failure_still_closes_database_and_other_resources(services):
    app, resources = services
    main.events_close.side_effect = RuntimeError("events cleanup failed")
    with pytest.raises(RuntimeError, match="events cleanup failed"):
        async with main.lifespan(app):
            pass
    main.close_pool.assert_awaited_once()
    for resource in resources.values():
        resource.close.assert_awaited_once()
    assert not any(t.get_name() == "session-cleanup" for t in asyncio.all_tasks())


async def test_shutdown_awaits_owned_session_janitor(services):
    app, _ = services
    before = asyncio.all_tasks()
    async with main.lifespan(app):
        tasks = asyncio.all_tasks() - before
        assert tasks
    assert all(t.done() for t in tasks)


async def test_real_proactive_catchup_is_joined_before_database_close(
    services, monkeypatch
):
    from app.proactive import ProactiveEngine

    app, _ = services
    engine = ProactiveEngine()
    engine._scheduler = MagicMock()
    entered, release = asyncio.Event(), asyncio.Event()
    tasks = []

    async def catchup():
        tasks.append(asyncio.current_task())
        entered.set()
        await release.wait()

    monkeypatch.setattr(engine, "_catch_up_missed", catchup)
    monkeypatch.setattr(main, "proactive", engine)

    async def close_pool():
        assert tasks and all(t.done() for t in tasks)

    monkeypatch.setattr(main, "close_pool", close_pool)
    try:
        async with main.lifespan(app):
            await asyncio.wait_for(entered.wait(), 1)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
