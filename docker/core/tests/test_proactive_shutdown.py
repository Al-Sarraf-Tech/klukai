"""Exercise real scheduler and task ownership without providers or databases."""

import asyncio
from unittest.mock import AsyncMock

from app.proactive import ProactiveEngine


async def test_close_joins_actual_scheduler_job_and_its_finalizer():
    engine = ProactiveEngine()
    started, finished = asyncio.Event(), asyncio.Event()

    async def job():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            finished.set()

    engine._add_job(job, "date", id="shutdown-proof")
    engine._scheduler.start()
    try:
        await asyncio.wait_for(started.wait(), 1)
        await engine.close()
        assert finished.is_set()
        assert not engine._scheduler.running
    finally:
        if engine._scheduler.running:
            engine.stop()


async def test_catchup_parent_stays_owned_after_nested_scheduled_job_returns(
    monkeypatch,
):
    engine = ProactiveEngine()
    entered, finished = asyncio.Event(), asyncio.Event()
    child = AsyncMock()
    engine._add_job(child, "interval", hours=1, id="child")

    async def parent():
        await engine._scheduler.get_job("child").func()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    monkeypatch.setattr(engine, "_catch_up_missed", parent)
    engine.start()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await engine.close()
        assert finished.is_set()
    finally:
        if engine._scheduler.running:
            engine.stop()


async def test_shutdown_refuses_new_listener_or_mission_background_work():
    engine = ProactiveEngine()
    await engine.close()
    ran = []

    async def late():
        ran.append(True)

    assert engine._spawn(late()) is None
    engine.start_mission("must stay stopped", user_id="alice")
    await asyncio.sleep(0)
    assert ran == []
    assert not engine.get_mission_timer("alice")


async def test_scheduler_stop_failure_still_joins_owned_work(monkeypatch):
    import pytest
    from types import SimpleNamespace

    engine = ProactiveEngine()
    entered, finalized = asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            finalized.set()

    task = engine._spawn(work())

    def fail(**_kw):
        raise RuntimeError("scheduler stop failed")

    monkeypatch.setattr(
        engine, "_scheduler", SimpleNamespace(running=True, shutdown=fail)
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(RuntimeError, match="scheduler stop failed"):
            await engine.close()
        assert task.done() and finalized.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_scheduler_error_audit_does_not_persist_query_credentials(monkeypatch):
    from apscheduler.events import EVENT_JOB_ERROR
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    engine = ProactiveEngine()
    engine._scheduler = MagicMock()
    monkeypatch.setattr(engine, "_catch_up_missed", AsyncMock())
    audit = AsyncMock()
    monkeypatch.setattr("app.audit.log", audit)
    engine.start()
    try:
        callback = next(
            call.args[0]
            for call in engine._scheduler.add_listener.call_args_list
            if call.args[1] == EVENT_JOB_ERROR
        )
        callback(
            SimpleNamespace(
                job_id="fixture",
                exception=ValueError("/ws?token=audit-fixture&keep=yes"),
                traceback="fixture",
            )
        )
        await asyncio.sleep(0)
        metadata = audit.call_args.kwargs["metadata"]
        assert "audit-fixture" not in metadata["exception"]
        assert "keep=yes" in metadata["exception"]
    finally:
        await engine.close()
