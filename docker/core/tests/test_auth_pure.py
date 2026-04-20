"""Auth module tests — IP banning, token lookup, session cleanup.

These tests mock the DB layer to exercise auth logic without a real Postgres.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _FakeConn:
    def __init__(self, fetchone_result=None):
        self._fetchone = fetchone_result
        self.executed: list[tuple] = []

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        result = AsyncMock()
        if self._fetchone is not None:
            result.fetchone = AsyncMock(return_value=self._fetchone)
        result.fetchall = AsyncMock(return_value=[])
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


# ═══════════════════════════════════════════════════════════════════════════
# check_ip_banned
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckIpBanned:
    @pytest.mark.asyncio
    async def test_not_banned_with_few_attempts(self):
        from app.auth import check_ip_banned
        conn = _FakeConn(fetchone_result=(2,))  # 2 failed, below threshold
        with patch("app.auth.get_conn", return_value=conn):
            assert await check_ip_banned("1.2.3.4") is False

    @pytest.mark.asyncio
    async def test_banned_at_threshold(self):
        from app.auth import check_ip_banned, IP_BAN_THRESHOLD
        conn = _FakeConn(fetchone_result=(IP_BAN_THRESHOLD,))
        with patch("app.auth.get_conn", return_value=conn):
            assert await check_ip_banned("1.2.3.4") is True

    @pytest.mark.asyncio
    async def test_banned_above_threshold(self):
        from app.auth import check_ip_banned, IP_BAN_THRESHOLD
        conn = _FakeConn(fetchone_result=(IP_BAN_THRESHOLD + 5,))
        with patch("app.auth.get_conn", return_value=conn):
            assert await check_ip_banned("1.2.3.4") is True

    @pytest.mark.asyncio
    async def test_fails_closed_on_db_error_returns_false(self):
        """Can't verify ban — fail-open (let request through). Logs warning."""
        from app.auth import check_ip_banned

        def broken():
            raise RuntimeError("db down")

        with patch("app.auth.get_conn", side_effect=broken):
            # Current impl: returns False on error (allows request)
            assert await check_ip_banned("1.2.3.4") is False


# ═══════════════════════════════════════════════════════════════════════════
# get_user_from_token
# ═══════════════════════════════════════════════════════════════════════════


class TestGetUserFromToken:
    @pytest.mark.asyncio
    async def test_valid_token_returns_user_id(self):
        from app.auth import get_user_from_token
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        conn = _FakeConn(fetchone_result=("alice", future))
        with patch("app.auth.get_conn", return_value=conn):
            user = await get_user_from_token("good-token")
        assert user == "alice"

    @pytest.mark.asyncio
    async def test_expired_token_returns_none(self):
        from app.auth import get_user_from_token
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        conn = _FakeConn(fetchone_result=("alice", past))
        with patch("app.auth.get_conn", return_value=conn):
            user = await get_user_from_token("expired-token")
        assert user is None

    @pytest.mark.asyncio
    async def test_missing_token_returns_none(self):
        from app.auth import get_user_from_token
        conn = _FakeConn(fetchone_result=None)
        with patch("app.auth.get_conn", return_value=conn):
            user = await get_user_from_token("bogus-token")
        assert user is None

    @pytest.mark.asyncio
    async def test_naive_expires_coerced_to_utc(self):
        """Expires timestamps without tz should still compare correctly (assumed UTC)."""
        from app.auth import get_user_from_token
        future_naive = datetime.utcnow() + timedelta(hours=1)  # deliberately naive
        conn = _FakeConn(fetchone_result=("bob", future_naive))
        with patch("app.auth.get_conn", return_value=conn):
            user = await get_user_from_token("tok")
        assert user == "bob"

    @pytest.mark.asyncio
    async def test_db_error_returns_none_not_raises(self):
        from app.auth import get_user_from_token

        def broken():
            raise RuntimeError("db down")

        with patch("app.auth.get_conn", side_effect=broken):
            user = await get_user_from_token("tok")
        assert user is None


# ═══════════════════════════════════════════════════════════════════════════
# cleanup_expired_sessions
# ═══════════════════════════════════════════════════════════════════════════


class TestCleanupExpiredSessions:
    @pytest.mark.asyncio
    async def test_returns_deleted_count(self):
        from app.auth import cleanup_expired_sessions
        conn = MagicMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=None)
        cur = AsyncMock()
        cur.fetchall = AsyncMock(return_value=[("tok-1",), ("tok-2",)])
        conn.execute = AsyncMock(return_value=cur)

        with patch("app.auth.get_conn_autocommit", return_value=conn):
            count = await cleanup_expired_sessions()
        assert count == 2

    @pytest.mark.asyncio
    async def test_zero_on_db_error(self):
        from app.auth import cleanup_expired_sessions

        def broken():
            raise RuntimeError("db down")

        with patch("app.auth.get_conn_autocommit", side_effect=broken):
            count = await cleanup_expired_sessions()
        assert count == 0


# ═══════════════════════════════════════════════════════════════════════════
# create_affection_for_user — thin wrapper, just verify it forwards
# ═══════════════════════════════════════════════════════════════════════════


class TestCreateAffectionForUser:
    @pytest.mark.asyncio
    async def test_delegates_to_ensure_row(self):
        from app import auth
        captured: list[str] = []

        async def fake_ensure(uid):
            captured.append(uid)

        with patch.object(auth, "_ensure_affection_row", side_effect=fake_ensure):
            await auth.create_affection_for_user("new_user")

        assert captured == ["new_user"]
