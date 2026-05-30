"""Integration test fixtures — real PG/Redis/Qdrant via the running stack.

These tests only run inside companion-core (or any environment with
DATABASE_URL + REDIS_URL + QDRANT_URL reachable). On a dev workstation
without the stack, they autoskip.

LM Studio is mocked at the router level so chat tests stay deterministic;
every other backend is real.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# ── Stack reachability gate ─────────────────────────────────────────────────


def _tcp_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _backends_reachable() -> tuple[bool, str]:
    """Return (ok, reason). Probe DSN host:port for PG, Redis, Qdrant."""
    db = os.environ.get("DATABASE_URL", "postgresql://aichat:aichat@aichat-db:5432/aichat")
    redis_url = os.environ.get("REDIS_URL", "redis://aichat-redis:6379/1")
    qdrant_url = os.environ.get("QDRANT_URL", "http://aichat-vector:6333")
    for url, default_port, name in (
        (db, 5432, "postgres"),
        (redis_url, 6379, "redis"),
        (qdrant_url, 6333, "qdrant"),
    ):
        u = urlparse(url)
        host = u.hostname or "localhost"
        port = u.port or default_port
        if not _tcp_open(host, port):
            return False, f"{name} unreachable at {host}:{port}"
    return True, "ok"


_STACK_OK, _STACK_REASON = _backends_reachable()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_marker = pytest.mark.skip(reason=f"integration stack not reachable: {_STACK_REASON}")
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)
            if not _STACK_OK:
                item.add_marker(skip_marker)


# ── LM Studio mock — only the external HTTP call ────────────────────────────


@pytest.fixture
def mock_llm_router() -> Iterator[AsyncMock]:
    """Replace app.context.router.stream + .route with deterministic stubs.

    `stream` is a real async-generator function (NOT AsyncMock) because the
    callers do `async for tok in router.stream(...)` — that needs an iterable
    return, not a coroutine.
    """

    async def fake_stream(*args: Any, **kwargs: Any):
        for tok in ["Welcome", " back, ", "Commander."]:
            yield tok

    fake_route = AsyncMock(return_value={
        "model": "test-model",
        "endpoint": "test://lmstudio",
        "max_tokens": 256,
        "temperature": 0.7,
    })

    with patch("app.context.router.stream", new=fake_stream), \
         patch("app.context.router.route", new=fake_route), \
         patch("app.context.router.init", new=AsyncMock()), \
         patch("app.context.router.close", new=AsyncMock()), \
         patch("app.context.router.keepalive", new=AsyncMock()):
        yield fake_route


# ── Test user fixture — created once per session, never deletes chat ────────


@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Module-scope loop so test client + asyncio fixtures share state."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def test_user_id() -> str:
    """Random per-run user id. Never collides with `jalsarraf`/`commander`."""
    return f"itest_{secrets.token_hex(6)}"


@pytest.fixture(scope="session")
def test_password() -> str:
    return secrets.token_urlsafe(20)


@pytest.fixture(scope="session")
async def _create_test_user(test_user_id: str, test_password: str) -> AsyncIterator[None]:
    """Insert + tear down user row. SACRED data (chat/episodes/affection)
    written under this user stays — only the user account row is deleted.
    """
    if not _STACK_OK:
        yield
        return

    import bcrypt
    from app.db import get_conn_autocommit, init_pool

    await init_pool()
    pw_hash = bcrypt.hashpw(test_password.encode(), bcrypt.gensalt()).decode()
    try:
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "INSERT INTO companion_users (id, username, password_hash, display_name) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (test_user_id, test_user_id, pw_hash, "Integration Test User"),
            )
    except Exception:
        pass
    yield
    try:
        async with get_conn_autocommit() as conn:
            # Delete sessions first: companion_auth_sessions has a FK to
            # companion_users, so deleting the user while a test created a
            # session (any login) would FK-violate and orphan the test account.
            await conn.execute("DELETE FROM companion_auth_sessions WHERE user_id = %s", (test_user_id,))
            await conn.execute("DELETE FROM companion_users WHERE id = %s", (test_user_id,))
    except Exception:
        pass


@pytest.fixture
async def auth_token(test_user_id: str, test_password: str, _create_test_user) -> str:
    """Login via real auth path, return bearer token."""
    from app.auth import authenticate
    tok = await authenticate(test_user_id, test_password, "127.0.0.1")
    assert tok, "Auth failed in integration setup"
    return tok


# ── FastAPI app + TestClient ────────────────────────────────────────────────


@pytest.fixture(scope="session")
def fastapi_app():
    """Import the real app object (with lifespan). Skip if stack absent."""
    if not _STACK_OK:
        pytest.skip(_STACK_REASON)
    from app.main import app
    return app


@pytest.fixture
def client(fastapi_app, mock_llm_router):
    """TestClient with lifespan=on so startup actually fires."""
    from fastapi.testclient import TestClient
    with TestClient(fastapi_app) as c:
        yield c
