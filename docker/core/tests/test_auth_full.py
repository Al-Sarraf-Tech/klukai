"""Tests for app.auth — authenticate, check_ip_banned, get_user_from_token.

Mocks DB + bcrypt to exercise the auth flow without real Postgres.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import auth


def _make_conn_ctx(fetchone_side_effect=None, fetchone_return=None):
    """Build a get_conn / get_conn_autocommit context manager mock.

    fetchone_side_effect: iterable for sequential .fetchone() calls.
    fetchone_return: single value if no side_effect.
    """
    if fetchone_side_effect is not None:
        result = AsyncMock()
        result.fetchone = AsyncMock(side_effect=fetchone_side_effect)
    else:
        result = AsyncMock()
        result.fetchone = AsyncMock(return_value=fetchone_return)

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=result)
    conn.commit = AsyncMock()

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock()
    return ctx, conn


class TestAuthenticate:
    @pytest.mark.asyncio
    async def test_returns_none_on_db_error(self):
        with patch("app.auth.get_conn_autocommit", side_effect=RuntimeError("db down")):
            token = await auth.authenticate("alice", "pw", "1.2.3.4")
        assert token is None

    @pytest.mark.asyncio
    async def test_returns_none_on_user_not_found(self):
        ctx, _ = _make_conn_ctx(fetchone_return=None)
        with patch("app.auth.get_conn_autocommit", return_value=ctx):
            token = await auth.authenticate("unknown", "pw", "1.2.3.4")
        assert token is None

    @pytest.mark.asyncio
    async def test_returns_none_on_wrong_password(self):
        # Real-looking bcrypt hash that won't match "wrong-pw"
        import bcrypt
        right_hash = bcrypt.hashpw(b"correct-pw", bcrypt.gensalt()).decode()
        ctx, _ = _make_conn_ctx(fetchone_return=("user-1", right_hash))
        with patch("app.auth.get_conn_autocommit", return_value=ctx):
            token = await auth.authenticate("alice", "wrong-pw", "1.2.3.4")
        assert token is None

    @pytest.mark.asyncio
    async def test_returns_token_on_success(self):
        import bcrypt
        plain = "the-right-pw"  # test fixture, not a real secret
        hashed = bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()
        ctx, _ = _make_conn_ctx(fetchone_return=("user-1", hashed))
        with patch("app.auth.get_conn_autocommit", return_value=ctx):
            token = await auth.authenticate("alice", plain, "1.2.3.4")
        assert token is not None
        assert isinstance(token, str)
        assert len(token) > 30  # url-safe 48 bytes

    @pytest.mark.asyncio
    async def test_audit_log_failure_swallowed(self):
        """Audit log failure during auth doesn't break the login flow."""
        import bcrypt
        plain = "pw-fixture"  # test fixture
        hashed = bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()
        ctx, _ = _make_conn_ctx(fetchone_return=("u-1", hashed))
        with patch("app.auth.get_conn_autocommit", return_value=ctx), \
             patch("app.audit.log", side_effect=RuntimeError("audit down")):
            token = await auth.authenticate("alice", plain, "1.2.3.4")
        assert token is not None


class TestCheckIpBanned:
    @pytest.mark.asyncio
    async def test_returns_false_when_count_low(self):
        ctx, _ = _make_conn_ctx(fetchone_return=(1,))
        with patch("app.auth.get_conn", return_value=ctx):
            banned = await auth.check_ip_banned("1.2.3.4")
        assert banned is False

    @pytest.mark.asyncio
    async def test_returns_true_when_threshold_exceeded(self):
        ctx, _ = _make_conn_ctx(fetchone_return=(auth.IP_BAN_THRESHOLD + 1,))
        with patch("app.auth.get_conn", return_value=ctx):
            banned = await auth.check_ip_banned("1.2.3.4")
        assert banned is True

    @pytest.mark.asyncio
    async def test_returns_false_on_db_error(self):
        # Fails-safe: DB error means "not banned" (don't lock people out)
        with patch("app.auth.get_conn", side_effect=RuntimeError("db down")):
            banned = await auth.check_ip_banned("1.2.3.4")
        assert banned is False


class TestGetUserFromToken:
    @pytest.mark.asyncio
    async def test_returns_none_on_db_error(self):
        with patch("app.auth.get_conn_autocommit", side_effect=RuntimeError("db down")):
            result = await auth.get_user_from_token("any")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_unknown_token(self):
        ctx, _ = _make_conn_ctx(fetchone_return=None)
        with patch("app.auth.get_conn_autocommit", return_value=ctx):
            result = await auth.get_user_from_token("bogus-token")
        assert result is None


class TestGetSessionInfo:
    @pytest.mark.asyncio
    async def test_returns_none_for_invalid_token(self):
        ctx, _ = _make_conn_ctx(fetchone_return=None)
        with patch("app.auth.get_conn", return_value=ctx):
            info = await auth.get_session_info("bogus")
        assert info is None

    @pytest.mark.asyncio
    async def test_returns_none_on_db_error(self):
        with patch("app.auth.get_conn", side_effect=RuntimeError("db down")):
            info = await auth.get_session_info("any")
        assert info is None


class TestChangePassword:
    @pytest.mark.asyncio
    async def test_returns_false_on_db_error(self):
        with patch("app.auth.get_conn_autocommit", side_effect=RuntimeError("db down")):
            ok = await auth.change_password("alice", "old", "newpassword12")
        assert ok is False

    @pytest.mark.asyncio
    async def test_returns_false_on_wrong_old_password(self):
        import bcrypt
        right_hash = bcrypt.hashpw(b"correct-old", bcrypt.gensalt()).decode()
        ctx, _ = _make_conn_ctx(fetchone_return=(right_hash,))
        with patch("app.auth.get_conn_autocommit", return_value=ctx):
            ok = await auth.change_password("alice", "wrong-old", "newpw123")
        assert ok is False

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        import bcrypt
        old_plain = "old-pw-1234"  # test fixture
        right_hash = bcrypt.hashpw(old_plain.encode(), bcrypt.gensalt()).decode()
        ctx, _ = _make_conn_ctx(fetchone_return=(right_hash,))
        with patch("app.auth.get_conn_autocommit", return_value=ctx):
            ok = await auth.change_password("alice", old_plain, "newpw-with-length-12")
        assert ok is True


class TestCleanupExpiredSessions:
    @pytest.mark.asyncio
    async def test_returns_zero_on_db_error(self):
        with patch("app.auth.get_conn_autocommit", side_effect=RuntimeError("db down")):
            count = await auth.cleanup_expired_sessions()
        assert count == 0

    @pytest.mark.asyncio
    async def test_returns_deleted_count(self):
        # cleanup uses cur.fetchall() and returns len(rows)
        result = AsyncMock()
        result.fetchall = AsyncMock(return_value=[
            ("token-1",), ("token-2",), ("token-3",), ("token-4",), ("token-5",),
        ])
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=result)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock()
        with patch("app.auth.get_conn_autocommit", return_value=ctx):
            count = await auth.cleanup_expired_sessions()
        assert count == 5
