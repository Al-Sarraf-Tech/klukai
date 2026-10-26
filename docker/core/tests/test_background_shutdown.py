"""Background entrypoints and native frame writes obey application shutdown."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app import db, main, portrait, portrait_account, push
from tests.test_application_shutdown import application as application


@pytest.mark.asyncio
async def test_push_subscription_delete_finishes_before_database_close(
    application, monkeypatch
):
    _, state, _ = application
    entered = asyncio.Event()
    finalizers = []

    async def execute(query, args):
        assert query.startswith("DELETE FROM companion_push_subscriptions")
        assert args == ("https://test.invalid/retired",)
        entered.set()
        await asyncio.Event().wait()

    @asynccontextmanager
    async def connection():
        try:
            yield SimpleNamespace(execute=execute)
        finally:
            finalizers.append(state["database_closed"])

    monkeypatch.setattr(db, "get_conn_autocommit", connection)
    monkeypatch.setattr(push, "_subscriptions", {})
    async with main.lifespan(main.app):
        push.remove_subscription("https://test.invalid/retired")
        await entered.wait()
    assert finalizers == [False]


@pytest.mark.asyncio
async def test_portrait_background_entrypoint_settles_before_clients_close(
    application, monkeypatch
):
    _, state, _ = application
    entered = asyncio.Event()
    finalizers = []

    async def work(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            finalizers.append(state["database_closed"])

    monkeypatch.setattr(portrait, "_inflight", {})
    monkeypatch.setattr(portrait, "_failed_at", {})
    monkeypatch.setattr(portrait, "_chain", work)
    monkeypatch.setattr(portrait_account, "enabled_for", lambda user_id: True)
    async with main.lifespan(main.app):
        assert portrait.ensure_generation("owner", "speed_star", 9)
        await entered.wait()
    assert finalizers == [False]
