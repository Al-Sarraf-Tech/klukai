"""Background entrypoints and native frame writes obey application shutdown."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from app import db, image_gen, main, portrait, push
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
    async with main.lifespan(main.app):
        assert portrait.ensure_generation("owner", "speed_star", 9)
        await entered.wait()
    assert finalizers == [False]


@pytest.mark.asyncio
async def test_cancelled_portrait_joins_native_frame_write_before_database_close(
    application, monkeypatch, tmp_path
):
    _, state, _ = application
    entered, release = threading.Event(), threading.Event()
    written = []
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    monkeypatch.setattr(portrait, "_inflight", {})
    monkeypatch.setattr(portrait, "_failed_at", {})
    monkeypatch.setattr(portrait, "_clear_to_render", AsyncMock(return_value=True))
    monkeypatch.setattr(
        portrait,
        "_SetPlan",
        lambda *args: SimpleNamespace(
            composite=lambda *args: Image.new("RGB", (1, 1)), accept=None, fallback=None
        ),
    )

    def write(path, value):
        entered.set()
        assert release.wait(3)
        written.append((path.name, state["database_closed"]))

    async def provider(**kwargs):
        await kwargs["on_image"]("blink", b"deterministic frame")
        await asyncio.Event().wait()

    monkeypatch.setattr(portrait, "_write_atomic", write)
    monkeypatch.setattr(image_gen, "generate_inpaint_set", provider)
    lifetime = main.lifespan(main.app)
    await lifetime.__aenter__()
    closing = None
    try:
        assert portrait.ensure_generation("owner", "speed_star", 9)
        assert await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(lifetime.__aexit__(None, None, None))
        await asyncio.sleep(0.02)
        assert not closing.done() and state["database_closed"] is False
    finally:
        release.set()
        if closing is None:
            await lifetime.__aexit__(None, None, None)
        else:
            await closing
    assert written == [("blink.webp", False)]
