"""Served background work must settle while its clients/database remain open."""

from __future__ import annotations

import asyncio
from builtins import ExceptionGroup
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app import auth, caches, job_handoff, main, rate_limit, wardrobe
from app.chat_prompt import ChatPromptBuilder
from app.models import SessionState
from app.observability import health_cache
from app.ws_manager import WSManager
from fastapi import FastAPI


@asynccontextmanager
async def empty_scope():
    yield


@pytest.fixture
def application(monkeypatch):
    state = {"database_closed": False}
    events = []
    manager = WSManager()

    async def close_pool():
        state["database_closed"] = True
        events.append("database_closed")

    async def init_pool(**kwargs):
        state["database_closed"] = False

    monkeypatch.setattr(main, "init_pool", init_pool)
    for name in ("run_migration", "events_init", "events_close"):
        monkeypatch.setattr(main, name, AsyncMock())
    monkeypatch.setattr(main, "close_pool", close_pool)
    monkeypatch.setattr(main, "ws", manager)
    for name in ("memory", "router", "mcp", "affection"):
        monkeypatch.setattr(
            main,
            name,
            SimpleNamespace(
                init=AsyncMock(), close=AsyncMock(), is_game_active=AsyncMock()
            ),
        )
    monkeypatch.setattr(main, "proactive", MagicMock(close=AsyncMock()))
    monkeypatch.setattr(main, "load_personality", MagicMock())
    monkeypatch.setattr(
        main.app.state, "voice_jobs", SimpleNamespace(close=AsyncMock())
    )
    for module, name in (
        (auth, "init_users"),
        (caches, "close"),
        (rate_limit, "close"),
    ):
        monkeypatch.setattr(module, name, AsyncMock())
    monkeypatch.setattr(health_cache, "running", empty_scope)
    monkeypatch.setattr(job_handoff, "running", empty_scope)
    try:
        from app import runtime_tasks
    except ImportError:
        pass
    else:
        monkeypatch.setattr(runtime_tasks, "SHUTDOWN_DRAIN_SECONDS", 0.001)
    return manager, state, events


@pytest.mark.asyncio
async def test_durable_image_finalizer_settles_before_database_close(application):
    manager, state, events = application
    started = asyncio.Event()
    observed = []

    async def accepted_image():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            observed.append(state["database_closed"])
            events.append("accepted_image_settled")

    task = None
    try:
        async with main.lifespan(main.app):
            task = asyncio.create_task(accepted_image())
            manager.track_task("owner", task, survive_disconnect=True)
            await started.wait()
            await manager.disconnect("owner")
            assert not task.done()  # Normal disconnect still preserves acceptance.
        assert task.done()
        assert observed == [False]
        assert events.index("accepted_image_settled") < events.index("database_closed")
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_shielded_wardrobe_pick_survives_budget_but_settles_before_shutdown(
    application, monkeypatch
):
    _, state, events = application
    started = asyncio.Event()
    settled = []

    async def pick(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            settled.append(state["database_closed"])

    monkeypatch.setattr(wardrobe, "ensure_today", pick)
    builder = ChatPromptBuilder(
        None, None, None, None, None, None, None, None, None, None, 0.001, 0.01
    )
    try:
        async with main.lifespan(main.app):
            await builder.wardrobe_turn(
                "hello", SessionState(conversation_id="owner"), "owner", 9, None
            )
            assert started.is_set() and settled == []
        assert settled == [False]
    finally:
        leftovers = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_coro().__qualname__ == pick.__qualname__
        ]
        for task in leftovers:
            task.cancel()
        await asyncio.gather(*leftovers, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_name", ["image_gen", "weather_client"]
)
async def test_lazy_http_clients_close_once_and_repeated_lifespan_gets_a_fresh_client(
    application, monkeypatch, module_name
):
    import importlib

    module = importlib.import_module("app." + module_name)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    close = AsyncMock(wraps=client.aclose)
    monkeypatch.setattr(client, "aclose", close)
    monkeypatch.setattr(module, "_http", client)
    async with main.lifespan(main.app):
        assert module._get_http() is client
    assert client.is_closed
    assert module._http is None
    await module.close()
    close.assert_awaited_once()
    async with main.lifespan(main.app):
        replacement = module._get_http()
        assert replacement is not client and not replacement.is_closed
    assert replacement.is_closed and module._http is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/mission", "/api/memories/backfill-annotations"])
async def test_http_created_background_work_finishes_before_database_close(
    application, monkeypatch, path
):
    from app import routes_extras

    _, state, _ = application
    started = asyncio.Event()
    tasks, finalizers = [], []

    async def work(*args, **kwargs):
        tasks.append(asyncio.current_task())
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            finalizers.append(state["database_closed"])

    monkeypatch.setattr(routes_extras, "_get_user_id", AsyncMock(return_value="owner"))
    monkeypatch.setattr(
        routes_extras.affection,
        "get_state",
        AsyncMock(return_value=SimpleNamespace(level=0)),
    )
    monkeypatch.setattr(routes_extras, "_run_mission", work)
    monkeypatch.setattr(routes_extras.memory_archive, "backfill_annotations", work)
    app = FastAPI()
    routes_extras.register_extras(app)
    try:
        async with main.lifespan(main.app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                assert (await client.post(path)).status_code == 200
            await started.wait()
        assert finalizers == [False]
        assert all(task.done() for task in tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_partial_startup_settles_spawned_work_before_resource_cleanup(
    application, monkeypatch
):
    from app.runtime_tasks import spawn

    _, state, events = application
    started = asyncio.Event()

    async def work():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert not state["database_closed"]
            events.append("work_settled")

    async def init():
        spawn(work())
        await started.wait()

    async def resource_close():
        assert "work_settled" in events
        events.append("resource_closed")

    monkeypatch.setattr(main.memory, "init", init)
    monkeypatch.setattr(main.memory, "close", resource_close)
    monkeypatch.setattr(
        main.router, "init", AsyncMock(side_effect=RuntimeError("partial startup"))
    )
    with pytest.raises(RuntimeError, match="partial startup"):
        async with main.lifespan(main.app):
            pytest.fail("Partial startup must not serve")
    assert events == ["work_settled", "resource_closed", "database_closed"]


@pytest.mark.asyncio
async def test_http_background_admission_remains_closed_during_client_cleanup(
    application, monkeypatch
):
    from app import routes_extras

    started = []
    statuses = []

    async def work(*args, **kwargs):
        started.append(True)

    monkeypatch.setattr(routes_extras, "_get_user_id", AsyncMock(return_value="owner"))
    monkeypatch.setattr(routes_extras.memory_archive, "backfill_annotations", work)
    app = FastAPI()
    routes_extras.register_extras(app)

    async def resource_close():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            statuses.append(
                (await client.post("/api/memories/backfill-annotations")).status_code
            )
        await asyncio.sleep(0)

    monkeypatch.setattr(main.memory, "close", resource_close)
    async with main.lifespan(main.app):
        pass
    assert statuses == [503] and started == []


@pytest.mark.asyncio
async def test_ws_task_created_during_shutdown_never_starts(application, monkeypatch):
    manager, _, _ = application
    ran = []
    tasks = []

    async def work():
        ran.append(True)

    async def resource_close():
        task = asyncio.create_task(work())
        tasks.append(task)
        manager.track_task("owner", task, survive_disconnect=True)
        await asyncio.sleep(0)

    monkeypatch.setattr(main.memory, "close", resource_close)
    async with main.lifespan(main.app):
        pass
    assert ran == [] and all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_existing_producer_owners_close_concurrently_before_resources(
    application, monkeypatch
):
    _, state, _ = application
    voice_started, proactive_started = asyncio.Event(), asyncio.Event()
    observed = []

    async def close_owner(started, other):
        started.set()
        try:
            await asyncio.wait_for(other.wait(), 0.05)
        except TimeoutError:
            observed.append((False, state["database_closed"]))
        else:
            observed.append((True, state["database_closed"]))

    monkeypatch.setattr(
        main.proactive,
        "close",
        lambda: close_owner(proactive_started, voice_started),
    )
    monkeypatch.setattr(
        main.app.state.voice_jobs,
        "close",
        lambda: close_owner(voice_started, proactive_started),
    )
    async with main.lifespan(main.app):
        pass
    assert observed == [(True, False), (True, False)]
    assert state["database_closed"]


@pytest.mark.asyncio
async def test_websocket_shutdown_cancels_ordinary_and_durable_work_together(
    application,
):
    manager, state, _ = application
    began = [asyncio.Event(), asyncio.Event()]
    finalizing = [asyncio.Event(), asyncio.Event()]
    observed = []

    async def work(index):
        began[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            finalizing[index].set()
            try:
                await asyncio.wait_for(finalizing[1 - index].wait(), 0.05)
            except TimeoutError:
                observed.append((False, state["database_closed"]))
            else:
                observed.append((True, state["database_closed"]))

    async with main.lifespan(main.app):
        await manager.connect(
            MagicMock(accept=AsyncMock(), close=AsyncMock(), send_text=AsyncMock()),
            "owner",
        )
        for index in range(2):
            task = asyncio.create_task(work(index))
            manager.track_task("owner", task, survive_disconnect=bool(index))
        await asyncio.gather(*(event.wait() for event in began))
    assert observed == [(True, False), (True, False)]
    assert state["database_closed"]


@pytest.mark.asyncio
async def test_failed_owner_does_not_skip_other_finalizers_or_close_database_early(
    application, monkeypatch
):
    _, state, _ = application
    started, release = asyncio.Event(), asyncio.Event()
    observed = []

    async def failed():
        raise RuntimeError("owner cleanup failed")

    async def finish():
        started.set()
        await release.wait()
        observed.append(state["database_closed"])

    monkeypatch.setattr(main.proactive, "close", failed)
    monkeypatch.setattr(main.app.state.voice_jobs, "close", finish)
    scope = main.lifespan(main.app)
    await scope.__aenter__()
    closing = asyncio.create_task(scope.__aexit__(None, None, None))
    try:
        await started.wait()
        await asyncio.sleep(0)
        assert not closing.done() and not state["database_closed"]
    finally:
        release.set()
        with pytest.raises(ExceptionGroup, match="shutdown failed"):
            await closing
    assert observed == [False] and state["database_closed"]
