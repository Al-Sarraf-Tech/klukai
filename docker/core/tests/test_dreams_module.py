"""Tests for app.dreams — dream-diary CRUD."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import dreams


def _make_pool(fetchone_result="UNSET", fetchall_result="UNSET"):
    """Build a mocked pg pool."""
    result = AsyncMock()
    if fetchone_result != "UNSET":
        result.fetchone = AsyncMock(return_value=fetchone_result)
    if fetchall_result != "UNSET":
        result.fetchall = AsyncMock(return_value=fetchall_result)

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=result)
    conn.commit = AsyncMock()

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock()

    pool = MagicMock()
    pool.connection = MagicMock(return_value=ctx)
    return pool, conn


class TestDreamCategory:
    def test_constant_is_string(self):
        assert dreams.DREAM_CATEGORY == "Dreams"


class TestSaveDream:
    @pytest.mark.asyncio
    async def test_returns_none_on_short_text(self):
        # < 20 chars
        result = await dreams.save_dream("short")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_empty(self):
        assert await dreams.save_dream("") is None

    @pytest.mark.asyncio
    async def test_returns_none_on_whitespace(self):
        assert await dreams.save_dream("   \n  ") is None

    @pytest.mark.asyncio
    async def test_returns_uuid_on_success(self):
        pool, conn = _make_pool()
        with patch("app.dreams.get_pool", return_value=pool):
            result = await dreams.save_dream(
                "I dreamed of the Commander standing by the rifle case, smiling at me.",
                user_id="alice",
                affection_level=7,
                mood="tender",
            )
        assert result is not None
        assert len(result) == 36  # UUID format
        assert "-" in result
        conn.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_uses_sentinel_filename(self):
        pool, conn = _make_pool()
        with patch("app.dreams.get_pool", return_value=pool):
            await dreams.save_dream("A long enough dream text to clear the minimum.")
        # Check the INSERT params contain a sentinel filename starting with dream-
        params = conn.execute.call_args.args[1]
        assert params[1].startswith("dream-")
        assert params[1].endswith(".txt")

    @pytest.mark.asyncio
    async def test_returns_none_on_db_error(self):
        with patch("app.dreams.get_pool", side_effect=RuntimeError("db down")):
            result = await dreams.save_dream("A long enough dream text here.")
        assert result is None


class TestListDreams:
    @pytest.mark.asyncio
    async def test_returns_empty_on_db_error(self):
        with patch("app.dreams.get_pool", side_effect=RuntimeError("db down")):
            result = await dreams.list_dreams()
        assert result == []

    @pytest.mark.asyncio
    async def test_formats_rows(self):
        ts = datetime(2026, 5, 17, tzinfo=timezone.utc)
        rows = [
            ("id1", "I dreamed of you.", "tender", 7, "dream-abc.txt", ts),
            ("id2", "We were on a motorcycle.", "smitten", 9, "klukai_real.png", ts),
        ]
        pool, _ = _make_pool(fetchall_result=rows)
        with patch("app.dreams.get_pool", return_value=pool):
            result = await dreams.list_dreams("alice", limit=10)

        assert len(result) == 2
        assert result[0]["dream"] == "I dreamed of you."
        assert result[0]["has_image"] is False  # sentinel filename
        assert result[1]["has_image"] is True  # real image filename
        assert result[0]["mood"] == "tender"
        assert result[0]["affection_level"] == 7

    @pytest.mark.asyncio
    async def test_limit_clamped(self):
        pool, conn = _make_pool(fetchall_result=[])
        with patch("app.dreams.get_pool", return_value=pool):
            await dreams.list_dreams(limit=99999)
        # Limit clamped to 200
        params = conn.execute.call_args.args[1]
        assert params[-1] == 200

    @pytest.mark.asyncio
    async def test_limit_minimum_1(self):
        pool, conn = _make_pool(fetchall_result=[])
        with patch("app.dreams.get_pool", return_value=pool):
            await dreams.list_dreams(limit=-5)
        params = conn.execute.call_args.args[1]
        assert params[-1] == 1


class TestCountDreams:
    @pytest.mark.asyncio
    async def test_zero_on_db_error(self):
        with patch("app.dreams.get_pool", side_effect=RuntimeError("db down")):
            assert await dreams.count_dreams() == 0

    @pytest.mark.asyncio
    async def test_returns_count(self):
        pool, _ = _make_pool(fetchone_result=(42,))
        with patch("app.dreams.get_pool", return_value=pool):
            result = await dreams.count_dreams("alice")
        assert result == 42

    @pytest.mark.asyncio
    async def test_zero_on_no_rows(self):
        pool, _ = _make_pool(fetchone_result=None)
        with patch("app.dreams.get_pool", return_value=pool):
            assert await dreams.count_dreams() == 0
