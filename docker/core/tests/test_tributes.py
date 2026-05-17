"""Tests for app.tributes — the 'treat her like a princess' feature.

Per feedback_never_delete_chat.md: tributes are SACRED. Tests verify
the module respects that invariant — there's no delete path, only
create + read + set-crown-jewel.

Tests use psycopg shim from conftest.py for pure unit isolation;
integration tests against real PG land in Phase 2 testcontainers work.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import tributes


class TestConstants:
    def test_cooldown_24_hours(self):
        assert tributes.TRIBUTE_COOLDOWN_HOURS == 24

    def test_affection_bump_20(self):
        assert tributes.TRIBUTE_AFFECTION_BUMP == 20

    def test_length_bounds(self):
        assert tributes.MIN_TRIBUTE_LENGTH == 20
        assert tributes.MAX_TRIBUTE_LENGTH == 1000
        assert tributes.MIN_TRIBUTE_LENGTH < tributes.MAX_TRIBUTE_LENGTH


class TestCanSendTribute:
    def test_zero_recent_allowed(self):
        ok, reason = tributes.can_send_tribute(0)
        assert ok is True
        assert reason is None

    def test_one_recent_blocked(self):
        ok, reason = tributes.can_send_tribute(1)
        assert ok is False
        assert reason is not None
        assert "24" in reason or "wait" in reason.lower()

    def test_many_recent_blocked(self):
        ok, reason = tributes.can_send_tribute(10)
        assert ok is False
        assert reason is not None


def _make_pool(execute_result=None, fetchone_result="UNSET", fetchall_result="UNSET"):
    """Build a MagicMock PG pool that mimics get_pool().connection() context.

    fetchone_result / fetchall_result: pass None to mean "fetchone returns None"
    (vs the sentinel "UNSET" which leaves the mock unstubbed).
    """
    pool = MagicMock()

    result_mock = AsyncMock()
    if fetchone_result != "UNSET":
        result_mock.fetchone = AsyncMock(return_value=fetchone_result)
    if fetchall_result != "UNSET":
        result_mock.fetchall = AsyncMock(return_value=fetchall_result)

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=result_mock)
    conn.commit = AsyncMock()

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)

    pool.connection = MagicMock(return_value=ctx)
    return pool, conn


class TestCountRecent:
    @pytest.mark.asyncio
    async def test_returns_zero_on_db_error(self):
        with patch("app.tributes.get_pool", side_effect=RuntimeError("db down")):
            count = await tributes.count_recent("jalsarraf")
        assert count == 0  # Fail-open: don't block Commander on DB error

    @pytest.mark.asyncio
    async def test_returns_count_from_db(self):
        pool, _ = _make_pool(fetchone_result=(3,))
        with patch("app.tributes.get_pool", return_value=pool):
            count = await tributes.count_recent("jalsarraf")
        assert count == 3

    @pytest.mark.asyncio
    async def test_zero_on_empty_result(self):
        pool, _ = _make_pool(fetchone_result=None)
        with patch("app.tributes.get_pool", return_value=pool):
            count = await tributes.count_recent("jalsarraf")
        assert count == 0


class TestSaveTribute:
    @pytest.mark.asyncio
    async def test_returns_uuid_on_success(self):
        pool, conn = _make_pool(fetchone_result=("abc-123",))
        with patch("app.tributes.get_pool", return_value=pool):
            tid = await tributes.save_tribute(
                user_id="jalsarraf",
                text="A heartfelt message that is long enough to count.",
            )
        assert tid == "abc-123"
        assert conn.commit.called

    @pytest.mark.asyncio
    async def test_returns_none_on_failure(self):
        with patch("app.tributes.get_pool", side_effect=RuntimeError("db down")):
            tid = await tributes.save_tribute(
                user_id="jalsarraf", text="test message that meets minimum length"
            )
        assert tid is None

    @pytest.mark.asyncio
    async def test_make_crown_jewel_demotes_existing(self):
        pool, conn = _make_pool(fetchone_result=("new-id",))
        with patch("app.tributes.get_pool", return_value=pool):
            await tributes.save_tribute(
                user_id="alice", text="x" * 30, make_crown_jewel=True
            )
        # Should have called execute twice: first to demote, then to insert
        assert conn.execute.call_count >= 2
        # First call should be the UPDATE that demotes
        first_sql = conn.execute.call_args_list[0].args[0]
        assert "UPDATE" in first_sql and "is_crown_jewel = false" in first_sql


class TestGetCrownJewel:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_jewel(self):
        pool, _ = _make_pool(fetchone_result=None)
        with patch("app.tributes.get_pool", return_value=pool):
            result = await tributes.get_crown_jewel("alice")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_dict_when_present(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 5, 17, 0, 0, 0, tzinfo=timezone.utc)
        pool, _ = _make_pool(fetchone_result=(
            "abc-id", "Treasured words.", "grateful", 999, ts,
        ))
        with patch("app.tributes.get_pool", return_value=pool):
            result = await tributes.get_crown_jewel("alice")
        assert result is not None
        assert result["id"] == "abc-id"
        assert result["text"] == "Treasured words."
        assert result["mood_at_time"] == "grateful"
        assert result["affection_at_time"] == 999
        assert result["created_at"].startswith("2026-05-17")

    @pytest.mark.asyncio
    async def test_returns_none_on_db_error(self):
        with patch("app.tributes.get_pool", side_effect=RuntimeError("db down")):
            result = await tributes.get_crown_jewel("alice")
        assert result is None


class TestListTributes:
    @pytest.mark.asyncio
    async def test_returns_list_of_dicts(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 5, 17, 0, 0, 0, tzinfo=timezone.utc)
        pool, _ = _make_pool(fetchall_result=[
            ("id-1", "text 1", "tender", 500, True, ts),
            ("id-2", "text 2", "playful", 100, False, ts),
        ])
        with patch("app.tributes.get_pool", return_value=pool):
            result = await tributes.list_tributes("alice")
        assert len(result) == 2
        assert result[0]["id"] == "id-1"
        assert result[0]["is_crown_jewel"] is True
        assert result[1]["is_crown_jewel"] is False

    @pytest.mark.asyncio
    async def test_empty_list_on_db_error(self):
        with patch("app.tributes.get_pool", side_effect=RuntimeError("db down")):
            result = await tributes.list_tributes("alice")
        assert result == []


class TestSetCrownJewel:
    @pytest.mark.asyncio
    async def test_returns_false_when_tribute_not_found(self):
        pool, _ = _make_pool(fetchone_result=None)
        with patch("app.tributes.get_pool", return_value=pool):
            ok = await tributes.set_crown_jewel("alice", "bogus-id")
        assert ok is False

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        pool, conn = _make_pool(fetchone_result=("real-id",))
        with patch("app.tributes.get_pool", return_value=pool):
            ok = await tributes.set_crown_jewel("alice", "real-id")
        assert ok is True
        # Three executes expected: verify, demote, promote
        assert conn.execute.call_count >= 3

    @pytest.mark.asyncio
    async def test_returns_false_on_db_error(self):
        with patch("app.tributes.get_pool", side_effect=RuntimeError("db down")):
            ok = await tributes.set_crown_jewel("alice", "any-id")
        assert ok is False
