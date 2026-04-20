"""Test /api/user/affection-timeline endpoint."""

from __future__ import annotations

import sys
from datetime import date
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


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, sql, params=None):
        result = AsyncMock()
        result.fetchall = AsyncMock(return_value=self._rows)
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    def connection(self):
        return _FakeConn(self._rows)


class TestAffectionTimeline:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/affection-timeline", "GET")
        with patch("app.routes._get_user_id", return_value=None):
            resp = await handler(_mk_request())
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_clamps_days(self):
        """days < 1 or > 365 should be clamped."""
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/affection-timeline", "GET")
        pool = _FakePool(rows=[])
        with patch("app.routes._get_user_id", return_value="alice"), \
             patch("app.routes.get_pool", return_value=pool):
            data = await handler(_mk_request(), days=-5)
        assert data["days"] == 1

        with patch("app.routes._get_user_id", return_value="alice"), \
             patch("app.routes.get_pool", return_value=pool):
            data = await handler(_mk_request(), days=9999)
        assert data["days"] == 365

    @pytest.mark.asyncio
    async def test_returns_point_list(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/affection-timeline", "GET")
        rows = [
            (date(2026, 4, 1), 100, 10, 3),
            (date(2026, 4, 2), 120, 20, 5),
            (date(2026, 4, 3), 125, 5,  2),
        ]
        pool = _FakePool(rows=rows)
        with patch("app.routes._get_user_id", return_value="alice"), \
             patch("app.routes.get_pool", return_value=pool):
            data = await handler(_mk_request(), days=30)

        assert data["days"] == 30
        assert data["count"] == 3
        assert data["points"][0]["end_score"] == 100
        assert data["points"][0]["net_delta"] == 10
        assert data["points"][0]["events"] == 3
        assert data["points"][0]["date"] == "2026-04-01"

    @pytest.mark.asyncio
    async def test_empty_timeline_for_new_user(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/affection-timeline", "GET")
        pool = _FakePool(rows=[])
        with patch("app.routes._get_user_id", return_value="new_user"), \
             patch("app.routes.get_pool", return_value=pool):
            data = await handler(_mk_request(), days=30)

        assert data["count"] == 0
        assert data["points"] == []

    @pytest.mark.asyncio
    async def test_db_error_returns_500(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/affection-timeline", "GET")

        def broken():
            raise RuntimeError("db down")

        with patch("app.routes._get_user_id", return_value="alice"), \
             patch("app.routes.get_pool", side_effect=broken):
            resp = await handler(_mk_request())
        assert resp.status_code == 500
