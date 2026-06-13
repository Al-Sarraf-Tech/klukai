"""Tests for memory + dream + audit route handlers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _mk_request() -> MagicMock:
    req = MagicMock()
    req.headers = {"Authorization": "Bearer good"}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    req.state = MagicMock()
    req.state.request_id = "test-req-id"
    return req


def _mk_unauth_request() -> MagicMock:
    req = MagicMock()
    req.headers = {}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    req.state = MagicMock()
    return req


def _app_with_routes() -> FastAPI:
    from app.routes import register_routes
    app = FastAPI()
    register_routes(app)
    return app


def _find_route(app: FastAPI, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


# ═══════════════════════════════════════════════════════════════════════════
# Memory archive routes
# ═══════════════════════════════════════════════════════════════════════════


class TestMemoriesListRoute:
    def test_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/memories", "GET") in paths

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_unauth_request())
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_passes_filters_through(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories", "GET")

        list_mock = AsyncMock(return_value=[{"id": "abc"}])
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.memory_archive.list_memories", new=list_mock):
            await handler(_mk_request(), category="combat", limit=10, before=None, month="2026-04")

        list_mock.assert_called_once()
        kwargs = list_mock.call_args.kwargs
        assert kwargs["category"] == "combat"
        assert kwargs["limit"] == 10
        assert kwargs["month"] == "2026-04"
        assert kwargs["user_id"] == "alice"


class TestMemoryCategoriesRoute:
    def test_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/memories/categories", "GET") in paths

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/categories", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_unauth_request())
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_passes_affection_level(self):
        from types import SimpleNamespace
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/categories", "GET")

        cats_mock = AsyncMock(return_value={"core": 5, "romantic": 2})
        aff = SimpleNamespace(level=7)
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.affection.get_state", new=AsyncMock(return_value=aff)), \
             patch("app.routes_extras.memory_archive.get_categories", new=cats_mock):
            await handler(_mk_request())

        cats_mock.assert_called_once_with(7, user_id="alice")


class TestMemoryTimelineRoute:
    def test_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/memories/timeline", "GET") in paths

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/timeline", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_unauth_request())
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_timeline(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/timeline", "GET")

        timeline = [{"month": "2026-04", "count": 12}]
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.memory_archive.get_timeline",
                   new=AsyncMock(return_value=timeline)):
            result = await handler(_mk_request())

        assert result == timeline


class TestMemoryKeepDiscardRoutes:
    def test_keep_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/memories/{memory_id}/keep", "POST") in paths

    def test_discard_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/memories/{memory_id}/discard", "POST") in paths


# ═══════════════════════════════════════════════════════════════════════════
# Dreams
# ═══════════════════════════════════════════════════════════════════════════


class TestDreamsRoute:
    def test_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/dreams", "GET") in paths


# ═══════════════════════════════════════════════════════════════════════════
# Messages
# ═══════════════════════════════════════════════════════════════════════════


class TestMessagesRoute:
    def test_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/messages", "GET") in paths

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/messages", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_unauth_request())
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_503_on_db_error(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/messages", "GET")

        class _BoomPool:
            def connection(self):
                raise RuntimeError("db down")

        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.get_pool", return_value=_BoomPool()):
            result = await handler(_mk_request(), limit=10, before=None)

        # On DB error the route returns 503 (not a fake-empty list) so the client
        # can show a retry instead of a conversation that looks wiped.
        assert result.status_code == 503


# ═══════════════════════════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════════════════════════


class TestAuditRoute:
    def test_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/audit", "GET") in paths


class TestAuditVerifyChainRoute:
    def test_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/audit/verify-chain", "GET") in paths
