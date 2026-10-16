"""Cancellation joins physical push work without resending an uncertain delivery."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
import sys
import threading
from types import SimpleNamespace

import pytest

from app import db, main, push, runtime_tasks
from tests.test_application_shutdown import application as application


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "gone", "failure"])
@pytest.mark.parametrize("cancellation", ["request", "shutdown"])
async def test_native_push_settles_before_database_close_without_retry_or_late_db_work(
    application, monkeypatch, outcome, cancellation, caplog
):
    _, state, _ = application
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls, native_finished, database_calls = [], [], []

    class WebPushException(Exception):
        response = SimpleNamespace(status_code=410)

    def webpush(**kwargs):
        calls.append(kwargs["subscription_info"]["endpoint"])
        started.set()
        try:
            assert release.wait(3)
            native_finished.append(state["database_closed"])
            if outcome == "gone":
                raise WebPushException("retired endpoint")
            if outcome == "failure":
                raise RuntimeError("offline synthetic native failure")
        finally:
            finished.set()

    async def execute(*args):
        database_calls.append(state["database_closed"])

    @asynccontextmanager
    async def connection():
        yield SimpleNamespace(execute=execute)

    monkeypatch.setitem(
        sys.modules,
        "pywebpush",
        SimpleNamespace(webpush=webpush, WebPushException=WebPushException),
    )
    monkeypatch.setattr(push, "_initialized", True)
    monkeypatch.setattr(push, "VAPID_PRIVATE_KEY", "offline-dummy")
    monkeypatch.setattr(
        push,
        "_subscriptions",
        {
            "owner": [
                {"endpoint": "https://fixture.invalid/first"},
                {"endpoint": "https://fixture.invalid/second"},
            ]
        },
    )
    monkeypatch.setattr(db, "get_conn_autocommit", connection)
    scope = main.lifespan(main.app)
    await scope.__aenter__()
    task = runtime_tasks.spawn(push.send_push("offline", "body", user_id="owner"))
    closing = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        if cancellation == "request":
            task.cancel()
            await asyncio.sleep(0)
        closing = asyncio.create_task(scope.__aexit__(None, None, None))
        async with asyncio.timeout(1):
            while not task.cancelling():
                await asyncio.sleep(0)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done() and not closing.done()
        assert not state["database_closed"] and not finished.is_set()
        release.set()
        await closing
        assert task.cancelled() and finished.is_set()
        assert native_finished == [False]
        assert calls == ["https://fixture.invalid/first"]
        assert (
            database_calls == []
        )  # A late410 is observed without scheduling stale DB work.
        assert not [
            record
            for record in caplog.records
            if record.name == "asyncio" and record.levelno >= logging.ERROR
        ]
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 1)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        else:
            await scope.__aexit__(None, None, None)
        await asyncio.gather(task, return_exceptions=True)
