"""Tests for dreams module + /api/dreams endpoint."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _FakeConn:
    def __init__(self, fetchall=None, fetchone=None):
        self._fetchall = fetchall or []
        self._fetchone = fetchone
        self.executed_sqls: list[str] = []
        self.executed_params: list[tuple] = []
        self.commits = 0

    async def execute(self, sql, params=None):
        self.executed_sqls.append(sql)
        self.executed_params.append(params or ())
        result = AsyncMock()
        result.fetchall = AsyncMock(return_value=self._fetchall)
        result.fetchone = AsyncMock(return_value=self._fetchone)
        return result

    async def commit(self):
        self.commits += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return self._conn


# ═══════════════════════════════════════════════════════════════════════════
# save_dream
# ═══════════════════════════════════════════════════════════════════════════


class TestSaveDream:
    @pytest.mark.asyncio
    async def test_empty_dream_returns_none(self):
        from app.dreams import save_dream
        assert await save_dream("") is None

    @pytest.mark.asyncio
    async def test_too_short_dream_returns_none(self):
        from app.dreams import save_dream
        assert await save_dream("tiny") is None

    @pytest.mark.asyncio
    async def test_saves_with_dreams_category(self):
        from app.dreams import save_dream, DREAM_CATEGORY
        conn = _FakeConn()
        pool = _FakePool(conn)
        with patch("app.dreams.get_pool", return_value=pool):
            mem_id = await save_dream(
                "A long enough dream description about an office rooftop.",
                user_id="alice",
                affection_level=5,
                mood="tender",
            )
        assert mem_id is not None
        # Verify category is Dreams in INSERT params
        params = conn.executed_params[0]
        assert DREAM_CATEGORY in params
        assert "alice" in params
        assert conn.commits == 1

    @pytest.mark.asyncio
    async def test_sentinel_filename_format(self):
        from app.dreams import save_dream
        conn = _FakeConn()
        pool = _FakePool(conn)
        with patch("app.dreams.get_pool", return_value=pool):
            await save_dream("A long enough dream description here.",
                             user_id="alice")
        params = conn.executed_params[0]
        # filename is the 2nd positional after id
        # (id, filename, annotation, category, mood, ...)
        filename = params[1]
        assert filename.startswith("dream-")
        assert filename.endswith(".txt")

    @pytest.mark.asyncio
    async def test_db_error_returns_none(self):
        from app.dreams import save_dream

        def broken():
            raise RuntimeError("db down")

        with patch("app.dreams.get_pool", side_effect=broken):
            result = await save_dream("A sufficiently long dream text to save.")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# list_dreams
# ═══════════════════════════════════════════════════════════════════════════


class TestListDreams:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_dreams(self):
        from app.dreams import list_dreams
        pool = _FakePool(_FakeConn(fetchall=[]))
        with patch("app.dreams.get_pool", return_value=pool):
            result = await list_dreams("alice")
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_sorted_newest_first(self):
        from app.dreams import list_dreams
        rows = [
            ("id1", "first dream text", "tender", 5, "dream-id1.txt",
             datetime(2026, 4, 19, tzinfo=timezone.utc)),
            ("id2", "second dream text", "composed", 5, "dream-id2.txt",
             datetime(2026, 4, 18, tzinfo=timezone.utc)),
        ]
        pool = _FakePool(_FakeConn(fetchall=rows))
        with patch("app.dreams.get_pool", return_value=pool):
            result = await list_dreams("alice", limit=10)
        assert len(result) == 2
        assert result[0]["dream"] == "first dream text"
        assert result[0]["has_image"] is False  # sentinel filename
        assert result[0]["mood"] == "tender"

    @pytest.mark.asyncio
    async def test_detects_image_when_non_sentinel_filename(self):
        from app.dreams import list_dreams
        rows = [
            ("id1", "a painted dream", "tender", 5, "abc123.png",
             datetime(2026, 4, 19, tzinfo=timezone.utc)),
        ]
        pool = _FakePool(_FakeConn(fetchall=rows))
        with patch("app.dreams.get_pool", return_value=pool):
            result = await list_dreams("alice")
        assert result[0]["has_image"] is True

    @pytest.mark.asyncio
    async def test_limit_clamped(self):
        from app.dreams import list_dreams
        conn = _FakeConn(fetchall=[])
        pool = _FakePool(conn)
        with patch("app.dreams.get_pool", return_value=pool):
            await list_dreams("alice", limit=9999)
        params = conn.executed_params[0]
        assert params[-1] <= 200  # clamp

    @pytest.mark.asyncio
    async def test_db_error_returns_empty(self):
        from app.dreams import list_dreams

        def broken():
            raise RuntimeError("db down")

        with patch("app.dreams.get_pool", side_effect=broken):
            assert await list_dreams("alice") == []


# ═══════════════════════════════════════════════════════════════════════════
# count_dreams
# ═══════════════════════════════════════════════════════════════════════════


class TestCountDreams:
    @pytest.mark.asyncio
    async def test_zero_when_none(self):
        from app.dreams import count_dreams
        pool = _FakePool(_FakeConn(fetchone=(0,)))
        with patch("app.dreams.get_pool", return_value=pool):
            assert await count_dreams("alice") == 0

    @pytest.mark.asyncio
    async def test_returns_count(self):
        from app.dreams import count_dreams
        pool = _FakePool(_FakeConn(fetchone=(7,)))
        with patch("app.dreams.get_pool", return_value=pool):
            assert await count_dreams("alice") == 7

    @pytest.mark.asyncio
    async def test_db_error_returns_zero(self):
        from app.dreams import count_dreams

        def broken():
            raise RuntimeError("db down")

        with patch("app.dreams.get_pool", side_effect=broken):
            assert await count_dreams("alice") == 0


# ═══════════════════════════════════════════════════════════════════════════
# /api/dreams endpoint
# ═══════════════════════════════════════════════════════════════════════════


def _mk_request(token: str | None = "good") -> MagicMock:
    req = MagicMock()
    req.headers = {"Authorization": f"Bearer {token}"} if token else {}
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


class TestDreamsEndpoint:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/dreams", "GET")
        with patch("app.routes._get_user_id", return_value=None):
            resp = await handler(_mk_request(None))
        assert resp.status_code == 401
        body = json.loads(resp.body)
        assert body["code"] == "AUTH_REQUIRED"

    @pytest.mark.asyncio
    async def test_returns_list_and_total(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/dreams", "GET")

        sample = [{"id": "a", "dream": "d1", "mood": "tender",
                   "affection_level": 5, "has_image": False,
                   "created_at": "2026-04-19T00:00:00+00:00"}]

        with patch("app.routes._get_user_id", return_value="alice"), \
             patch("app.dreams.list_dreams", new=AsyncMock(return_value=sample)), \
             patch("app.dreams.count_dreams", new=AsyncMock(return_value=5)):
            data = await handler(_mk_request())

        assert data["count"] == 1
        assert data["total"] == 5
        assert data["dreams"] == sample
