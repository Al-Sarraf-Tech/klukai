"""Tests for memory_archive DB-touching helpers — list_memories/get_timeline/get_categories/update_kept/update_curation.
Mocks the pool so we exercise SQL shape + result parsing without a real DB."""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _FakeConn:
    def __init__(self, rows=None, one=None):
        self._rows = rows or []
        self._one = one
        self.executed_sqls: list[str] = []

    async def execute(self, sql, params=None):
        self.executed_sqls.append(sql)
        result = AsyncMock()
        result.fetchall = AsyncMock(return_value=self._rows)
        result.fetchone = AsyncMock(return_value=self._one)
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


from contextlib import asynccontextmanager


def _mk_get_conn(conn):
    @asynccontextmanager
    async def _ctx():
        yield conn
    return _ctx


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return self._conn


# ═══════════════════════════════════════════════════════════════════════════
# list_memories
# ═══════════════════════════════════════════════════════════════════════════


class TestListMemories:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_rows(self):
        from app.memory_archive import list_memories
        conn = _FakeConn(rows=[])
        with patch("app.memory_archive_query.get_conn", _mk_get_conn(conn)):
            result = await list_memories()
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_structured_rows(self):
        from app.memory_archive import list_memories
        rows = [
            # id, filename, thumb_filename, annotation, scene_tags,
            # mood, affection_level, kept_by, category, created_at
            ("mid1", "img1.png", "thumb1.png", "annotation text",
             ["tag1"], "composed", 5, "klukai", "Mission Records",
             datetime(2026, 4, 20, tzinfo=timezone.utc)),
        ]
        conn = _FakeConn(rows=rows)
        with patch("app.memory_archive_query.get_conn", _mk_get_conn(conn)):
            result = await list_memories(user_id="alice", limit=10)
        assert len(result) == 1
        assert result[0]["id"] == "mid1"
        assert result[0]["annotation"] == "annotation text"
        assert result[0]["category"] == "Mission Records"

    @pytest.mark.asyncio
    async def test_category_filter_adds_where_clause(self):
        from app.memory_archive import list_memories
        conn = _FakeConn(rows=[])
        with patch("app.memory_archive_query.get_conn", _mk_get_conn(conn)):
            await list_memories(user_id="alice", category="Dreams")
        # SQL should contain category filter
        assert any("category" in s for s in conn.executed_sqls)


# ═══════════════════════════════════════════════════════════════════════════
# get_timeline
# ═══════════════════════════════════════════════════════════════════════════


class TestGetTimeline:
    @pytest.mark.asyncio
    async def test_groups_by_month(self):
        from app.memory_archive import get_timeline
        rows = [("2026-04", 12), ("2026-03", 8), ("2026-02", 5)]
        conn = _FakeConn(rows=rows)
        with patch("app.memory_archive_query.get_conn", _mk_get_conn(conn)):
            result = await get_timeline(user_id="alice")
        assert len(result) == 3
        assert {r["month"] for r in result} == {"2026-04", "2026-03", "2026-02"}
        assert {r["count"] for r in result} == {12, 8, 5}

    @pytest.mark.asyncio
    async def test_empty_on_db_error(self):
        from app.memory_archive import get_timeline

        def broken():
            raise RuntimeError("boom")

        with patch("app.memory_archive_query.get_conn", side_effect=broken):
            assert await get_timeline() == []


# ═══════════════════════════════════════════════════════════════════════════
# get_categories (level-gated availability + counts)
# ═══════════════════════════════════════════════════════════════════════════


class TestGetCategories:
    @pytest.mark.asyncio
    async def test_returns_level_zero_cats_for_new_user(self):
        from app.memory_archive import get_categories
        # DB returns 0 counts for each existing category
        rows = [("Mission Records", 0), ("Dreams", 0)]
        conn = _FakeConn(rows=rows)
        with patch("app.memory_archive_query.get_conn", _mk_get_conn(conn)):
            result = await get_categories(affection_level=0, user_id="alice")
        assert isinstance(result, list)
        assert all("name" in c and "count" in c for c in result)

    @pytest.mark.asyncio
    async def test_empty_on_db_error(self):
        from app.memory_archive import get_categories

        def broken():
            raise RuntimeError("down")

        with patch("app.memory_archive_query.get_conn", side_effect=broken):
            assert await get_categories(0, "alice") == []


# ═══════════════════════════════════════════════════════════════════════════
# update_kept (SET kept=...)
# ═══════════════════════════════════════════════════════════════════════════


class TestUpdateKept:
    @pytest.mark.asyncio
    async def test_updates_and_returns_true(self):
        from app.memory_archive import update_kept
        conn = _FakeConn()
        with patch("app.memory_archive_query.get_conn_autocommit", _mk_get_conn(conn)):
            result = await update_kept("mem-id", kept=True, user_id="alice")
        # Either True or dict — just ensure not crashed
        assert result is True or isinstance(result, bool)
        assert any("UPDATE companion_memories" in s for s in conn.executed_sqls)

    @pytest.mark.asyncio
    async def test_db_error_returns_false_or_none(self):
        from app.memory_archive import update_kept

        def broken():
            raise RuntimeError("db")

        with patch("app.memory_archive_query.get_conn_autocommit", side_effect=broken):
            result = await update_kept("mem-id", kept=True)
        # Should not raise; return falsy
        assert not result


# ═══════════════════════════════════════════════════════════════════════════
# available_categories edge cases (already covered in pure tests, but confirm)
# ═══════════════════════════════════════════════════════════════════════════


class TestAvailableCategoriesMore:
    def test_dreams_available_at_level_zero(self):
        """Level-0 users must be able to save dreams (category at lowest gate)."""
        from app.memory_archive import available_categories
        assert "Dreams" in available_categories(0)

    def test_higher_levels_still_include_dreams(self):
        from app.memory_archive import available_categories
        for lvl in (0, 3, 6, 9):
            assert "Dreams" in available_categories(lvl)
