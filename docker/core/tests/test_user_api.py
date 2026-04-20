"""Tests for new user-facing API endpoints: stats, export, memory-search.

These tests exercise the route handlers with mocked database/context to
verify auth gating, input validation, and response shape.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _mk_request(token: str | None = "good") -> MagicMock:
    req = MagicMock()
    req.headers = {"Authorization": f"Bearer {token}"} if token else {}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    return req


def _mk_affection(score: int = 500, level: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        score=score,
        level=level,
        level_name="Trust Established",
        consecutive_days=7,
        total_interactions=100,
        first_interaction=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _app_with_routes() -> FastAPI:
    """Build a fresh FastAPI app with routes registered (module-scoped import)."""
    from app.routes import register_routes
    app = FastAPI()
    register_routes(app)
    return app


# ═══════════════════════════════════════════════════════════════════════════
# Route registration (smoke)
# ═══════════════════════════════════════════════════════════════════════════


class TestRouteRegistration:
    def test_user_stats_route_registered(self):
        app = _app_with_routes()
        paths = {route.path for route in app.routes}
        assert "/api/user/stats" in paths

    def test_user_export_route_registered(self):
        app = _app_with_routes()
        paths = {route.path for route in app.routes}
        assert "/api/user/export" in paths

    def test_memory_search_route_registered(self):
        app = _app_with_routes()
        paths = {route.path for route in app.routes}
        assert "/api/memories/search" in paths


# ═══════════════════════════════════════════════════════════════════════════
# Auth gating — all 3 endpoints reject unauthenticated requests
# ═══════════════════════════════════════════════════════════════════════════


def _find_route(app: FastAPI, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


class TestAuthGating:
    @pytest.mark.asyncio
    async def test_stats_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/stats", "GET")
        with patch("app.routes._get_user_id", return_value=None):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_export_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/export", "GET")
        with patch("app.routes._get_user_id", return_value=None):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_memory_search_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/search", "GET")
        with patch("app.routes._get_user_id", return_value=None):
            resp = await handler(_mk_request(token=None), q="test")
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# Input validation
# ═══════════════════════════════════════════════════════════════════════════


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_memory_search_rejects_short_query(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/search", "GET")
        with patch("app.routes._get_user_id", return_value="alice"):
            resp = await handler(_mk_request(), q="a")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_memory_search_rejects_empty_query(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/search", "GET")
        with patch("app.routes._get_user_id", return_value="alice"):
            resp = await handler(_mk_request(), q="")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_memory_search_limit_clamped(self):
        """Limit should be clamped to 1..100."""
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/search", "GET")

        captured_limit: list[int] = []

        class FakePool:
            def connection(self):
                pool = self

                class Conn:
                    async def execute(self, sql, params):
                        captured_limit.append(params[-1])
                        result = AsyncMock()
                        result.fetchall = AsyncMock(return_value=[])
                        return result

                    async def __aenter__(self_inner):
                        return self_inner

                    async def __aexit__(self_inner, *a):
                        return None

                return Conn()

        with patch("app.routes._get_user_id", return_value="alice"), \
             patch("app.routes.get_pool", return_value=FakePool()):
            await handler(_mk_request(), q="valid", limit=1000)

        assert captured_limit and captured_limit[0] <= 100


# ═══════════════════════════════════════════════════════════════════════════
# Stats endpoint — response shape + DB aggregates
# ═══════════════════════════════════════════════════════════════════════════


class TestUserStats:
    @pytest.mark.asyncio
    async def test_stats_returns_expected_keys(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/stats", "GET")

        # Four sequential queries: messages/days/first/last; user_msg; mem counts; gift; firsts
        query_results = [
            (42, 5, datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 4, 20, tzinfo=timezone.utc)),
            (20,),       # user messages
            (10, 8, 5),  # total/kept/with_image memories
            (3,),        # gift count
            (4,),        # firsts count
        ]

        class FakeConn:
            def __init__(self):
                self._i = 0

            async def execute(self, sql, params):
                result = AsyncMock()
                result.fetchone = AsyncMock(return_value=query_results[self._i])
                self._i += 1
                return result

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

        class FakePool:
            def connection(self):
                return FakeConn()

        with patch("app.routes._get_user_id", return_value="alice"), \
             patch("app.routes.get_pool", return_value=FakePool()), \
             patch("app.routes.affection") as aff:
            aff.get_state = AsyncMock(return_value=_mk_affection())
            data = await handler(_mk_request())

        assert data["user_id"] == "alice"
        assert data["total_messages"] == 42
        assert data["user_messages"] == 20
        assert data["klukai_messages"] == 22
        assert data["days_active"] == 5
        assert data["memories"]["total"] == 10
        assert data["memories"]["kept"] == 8
        assert data["memories"]["with_image"] == 5
        assert data["gifts_given"] == 3
        assert data["milestones_reached"] == 4
        assert data["affection"]["level"] == 5
        assert "first_interaction" in data
        assert "last_interaction" in data

    @pytest.mark.asyncio
    async def test_stats_handles_empty_user(self):
        """New user with no activity should not crash."""
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/stats", "GET")

        query_results = [
            (0, 0, None, None),
            (0,),
            (0, 0, 0),
            (0,),
            (0,),
        ]

        class FakeConn:
            def __init__(self):
                self._i = 0

            async def execute(self, sql, params):
                result = AsyncMock()
                result.fetchone = AsyncMock(return_value=query_results[self._i])
                self._i += 1
                return result

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

        class FakePool:
            def connection(self):
                return FakeConn()

        with patch("app.routes._get_user_id", return_value="new_user"), \
             patch("app.routes.get_pool", return_value=FakePool()), \
             patch("app.routes.affection") as aff:
            aff.get_state = AsyncMock(return_value=_mk_affection(score=0, level=0))
            data = await handler(_mk_request())

        assert data["total_messages"] == 0
        assert data["first_interaction"] is None
        assert data["last_interaction"] is None
        assert data["memories"]["total"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# Export endpoint
# ═══════════════════════════════════════════════════════════════════════════


class TestUserExport:
    @pytest.mark.asyncio
    async def test_export_returns_full_bundle(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/export", "GET")

        msg_rows = [
            ("user", "hello", "text", "composed", None, datetime(2026, 1, 1, tzinfo=timezone.utc)),
            ("assistant", "hi", "text", "warm", "dolphin", datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)),
        ]
        firsts_rows = [
            ("first_message", datetime(2026, 1, 1, tzinfo=timezone.utc), {"context": "morning"}),
        ]
        gifts_rows = [
            ("flower", "pretty", "delighted", datetime(2026, 2, 1, tzinfo=timezone.utc)),
        ]
        memories_rows = [
            ("test annotation", "slice_of_life", ["tag1"], "test prompt",
             datetime(2026, 3, 1, tzinfo=timezone.utc)),
        ]
        row_batches = [msg_rows, firsts_rows, gifts_rows, memories_rows]

        class FakeConn:
            def __init__(self):
                self._i = 0

            async def execute(self, sql, params):
                batch = row_batches[self._i]
                self._i += 1
                result = AsyncMock()
                result.fetchall = AsyncMock(return_value=batch)
                return result

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

        class FakePool:
            def connection(self):
                return FakeConn()

        with patch("app.routes._get_user_id", return_value="alice"), \
             patch("app.routes.get_pool", return_value=FakePool()), \
             patch("app.routes.affection") as aff:
            aff.get_state = AsyncMock(return_value=_mk_affection())
            data = await handler(_mk_request())

        assert data["user_id"] == "alice"
        assert len(data["messages"]) == 2
        assert data["messages"][0]["role"] == "user"
        assert len(data["milestones"]) == 1
        assert data["milestones"][0]["event_type"] == "first_message"
        assert len(data["gifts"]) == 1
        assert data["gifts"][0]["item"] == "flower"
        assert len(data["memories_kept"]) == 1
        assert data["affection_snapshot"]["level"] == 5
        assert "exported_at" in data

    @pytest.mark.asyncio
    async def test_export_excludes_messages_when_flag_false(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/export", "GET")

        # Only 3 queries expected (no message query): firsts, gifts, memories
        row_batches = [[], [], []]

        class FakeConn:
            def __init__(self):
                self._i = 0

            async def execute(self, sql, params):
                batch = row_batches[self._i]
                self._i += 1
                result = AsyncMock()
                result.fetchall = AsyncMock(return_value=batch)
                return result

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

        class FakePool:
            def connection(self):
                return FakeConn()

        with patch("app.routes._get_user_id", return_value="alice"), \
             patch("app.routes.get_pool", return_value=FakePool()), \
             patch("app.routes.affection") as aff:
            aff.get_state = AsyncMock(return_value=_mk_affection())
            data = await handler(_mk_request(), include_messages=False)

        assert "messages" not in data
        assert "memories_kept" in data  # default True
        assert "milestones" in data
        assert "gifts" in data


# ═══════════════════════════════════════════════════════════════════════════
# Memory search endpoint
# ═══════════════════════════════════════════════════════════════════════════


class TestMemorySearch:
    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/search", "GET")

        rows = [
            ("m1", "img1.png", "she smiled at the station", "slice_of_life", ["tag"],
             datetime(2026, 1, 1, tzinfo=timezone.utc)),
            ("m2", "img2.png", "another smile moment", "slice_of_life", ["tag"],
             datetime(2026, 2, 1, tzinfo=timezone.utc)),
        ]

        class FakeConn:
            async def execute(self, sql, params):
                assert "ILIKE" in sql
                assert "kept = true" in sql
                result = AsyncMock()
                result.fetchall = AsyncMock(return_value=rows)
                return result

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

        class FakePool:
            def connection(self):
                return FakeConn()

        with patch("app.routes._get_user_id", return_value="alice"), \
             patch("app.routes.get_pool", return_value=FakePool()):
            data = await handler(_mk_request(), q="smile")

        assert data["query"] == "smile"
        assert data["count"] == 2
        assert len(data["results"]) == 2
        assert data["results"][0]["annotation"] == "she smiled at the station"
