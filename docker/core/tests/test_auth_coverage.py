"""Behavioral coverage top-up for app.auth.

Targets the previously-uncovered surface: init_users seed loop,
_ensure_affection_row, the audit-log best-effort swallow on a failed
login, the rolling-refresh UPDATE swallow in get_user_from_token, the
get_session_info empty-row branch, and the audit swallow in
change_password.

Every test asserts real behavior: which SQL ran, which params were
bound, and what the function returned. The DB pool and bcrypt are mocked;
no Postgres, no network, deterministic.

NOTE on scope: these tests do NOT relax any credential check. Passwords
are still verified against real bcrypt hashes (bcrypt is installed in the
venv), tokens are still required, and a wrong password / missing user
still yields a falsy result.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import bcrypt
import pytest

from app import auth


# ── DB pool mock helper ──────────────────────────────────────────────────────
def _make_conn(fetchone_side_effect=None, fetchone_return=None, fetchall_return=None):
    """Build a (ctx, conn) pair whose conn records every execute() call.

    conn.executed is a list of (sql, params) tuples for assertions.
    fetchone_side_effect: iterable, one result per execute().fetchone() call.
    """
    conn = MagicMock()
    conn.executed: list[tuple] = []

    if fetchone_side_effect is not None:
        it = iter(fetchone_side_effect)

        async def _execute(sql, params=None):
            conn.executed.append((sql, params))
            res = MagicMock()
            try:
                val = next(it)
            except StopIteration:
                val = None
            res.fetchone = AsyncMock(return_value=val)
            res.fetchall = AsyncMock(return_value=fetchall_return or [])
            return res
    else:
        async def _execute(sql, params=None):
            conn.executed.append((sql, params))
            res = MagicMock()
            res.fetchone = AsyncMock(return_value=fetchone_return)
            res.fetchall = AsyncMock(return_value=fetchall_return or [])
            return res

    conn.execute = _execute

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx, conn


# ═══════════════════════════════════════════════════════════════════════════
# init_users — seed-user creation loop (lines 38-74)
# ═══════════════════════════════════════════════════════════════════════════
class TestInitUsers:
    @pytest.mark.asyncio
    async def test_creates_missing_user_when_password_env_set(self):
        """A seed user with no DB row + a SEED_PASSWORD_<U> env var is INSERTed
        with a real bcrypt hash, and the password is never stored in plaintext."""
        # Every SELECT id returns None (no user / no affection row exists yet)
        ctx, conn = _make_conn(fetchone_return=None)
        env = {f"SEED_PASSWORD_{u['username'].upper()}": "" for u in auth._SEED_USERS}
        env["SEED_PASSWORD_JALSARRAF"] = "commander-pw-1234"  # only this one set
        with patch("app.auth.get_conn_autocommit", return_value=ctx), \
             patch.dict(os.environ, env, clear=False):
            await auth.init_users()

        inserts = [(s, p) for (s, p) in conn.executed if "INSERT INTO companion_users" in s]
        assert len(inserts) == 1, "only the env-backed user should be created"
        sql, params = inserts[0]
        # params = (id, username, pw_hash, display_name)
        assert params[0] == "jalsarraf"
        assert params[1] == "jalsarraf"
        # Stored value is a bcrypt hash, NOT the plaintext password
        assert params[2] != "commander-pw-1234"
        assert bcrypt.checkpw(b"commander-pw-1234", params[2].encode())

    @pytest.mark.asyncio
    async def test_skips_user_when_password_env_missing(self):
        """No SEED_PASSWORD_<U> set anywhere → zero users created (skip, not crash)."""
        ctx, conn = _make_conn(fetchone_return=None)
        # Clear all seed password env vars
        env = {f"SEED_PASSWORD_{u['username'].upper()}": "" for u in auth._SEED_USERS}
        with patch("app.auth.get_conn_autocommit", return_value=ctx), \
             patch.dict(os.environ, env, clear=False):
            await auth.init_users()

        inserts = [s for (s, _) in conn.executed if "INSERT INTO companion_users" in s]
        assert inserts == [], "missing password env must skip creation entirely"

    @pytest.mark.asyncio
    async def test_existing_user_not_reinserted(self):
        """If SELECT id returns a row, that user is skipped (continue branch)."""
        # First fetchone for each user returns an existing row id; affection row also exists.
        ctx, conn = _make_conn(fetchone_return=("exists",))
        with patch("app.auth.get_conn_autocommit", return_value=ctx):
            await auth.init_users()

        inserts = [s for (s, _) in conn.executed if "INSERT INTO companion_users" in s]
        assert inserts == [], "existing users must not be re-inserted"

    @pytest.mark.asyncio
    async def test_swallows_db_error(self):
        """A pool failure is logged and swallowed — startup must not crash."""
        with patch("app.auth.get_conn_autocommit", side_effect=RuntimeError("pool down")):
            # Must not raise
            await auth.init_users()


# ═══════════════════════════════════════════════════════════════════════════
# _ensure_affection_row (lines 79-97)
# ═══════════════════════════════════════════════════════════════════════════
class TestEnsureAffectionRow:
    @pytest.mark.asyncio
    async def test_inserts_row_when_absent(self):
        """No affection row → INSERT with score 0 / level 0 / Cold Assessment."""
        ctx, conn = _make_conn(fetchone_return=None)
        with patch("app.auth.get_conn_autocommit", return_value=ctx):
            await auth._ensure_affection_row("alice")

        inserts = [(s, p) for (s, p) in conn.executed if "INSERT INTO companion_affection" in s]
        assert len(inserts) == 1
        sql, params = inserts[0]
        assert "Cold Assessment" in sql
        assert params == ("alice",)

    @pytest.mark.asyncio
    async def test_no_insert_when_row_exists(self):
        """Existing affection row → no INSERT (idempotent)."""
        ctx, conn = _make_conn(fetchone_return=("affection-id",))
        with patch("app.auth.get_conn_autocommit", return_value=ctx):
            await auth._ensure_affection_row("alice")

        inserts = [s for (s, _) in conn.executed if "INSERT INTO companion_affection" in s]
        assert inserts == []

    @pytest.mark.asyncio
    async def test_swallows_db_error(self):
        """DB failure logged + swallowed (warning), never raised."""
        with patch("app.auth.get_conn_autocommit", side_effect=RuntimeError("db down")):
            await auth._ensure_affection_row("alice")  # must not raise

    @pytest.mark.asyncio
    async def test_create_affection_for_user_delegates(self):
        """create_affection_for_user forwards to _ensure_affection_row and inserts."""
        ctx, conn = _make_conn(fetchone_return=None)
        with patch("app.auth.get_conn_autocommit", return_value=ctx):
            await auth.create_affection_for_user("newbie")
        inserts = [(s, p) for (s, p) in conn.executed if "INSERT INTO companion_affection" in s]
        assert inserts and inserts[0][1] == ("newbie",)


# ═══════════════════════════════════════════════════════════════════════════
# authenticate — audit-log swallow on FAILED login (lines 153-154)
# ═══════════════════════════════════════════════════════════════════════════
class TestAuthenticateFailureAuditSwallow:
    @pytest.mark.asyncio
    async def test_failed_login_records_attempt_and_swallows_audit_error(self):
        """Wrong password: records a FALSE login attempt, audit.log raising is
        swallowed, and the function returns None (auth still fails closed)."""
        right_hash = bcrypt.hashpw(b"correct-pw", bcrypt.gensalt()).decode()
        ctx, conn = _make_conn(fetchone_return=("user-1", right_hash))
        with patch("app.auth.get_conn_autocommit", return_value=ctx), \
             patch("app.audit.log", new=AsyncMock(side_effect=RuntimeError("audit down"))):
            token = await auth.authenticate("alice", "wrong-pw", "9.9.9.9")

        assert token is None  # security: wrong password must not authenticate
        # A FALSE login attempt was recorded
        attempt_inserts = [
            (s, p) for (s, p) in conn.executed
            if "companion_login_attempts" in s
        ]
        assert any("FALSE" in s for (s, _) in attempt_inserts)
        # No session token row was created
        assert not any("companion_auth_sessions" in s for (s, _) in conn.executed)

    @pytest.mark.asyncio
    async def test_success_records_session_and_swallows_audit_error(self):
        """Correct password still issues a token even when the success audit
        log raises (lines 134-139 best-effort path)."""
        plain = "right-pw-9999"
        hashed = bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()
        ctx, conn = _make_conn(fetchone_return=("user-7", hashed))
        with patch("app.auth.get_conn_autocommit", return_value=ctx), \
             patch("app.audit.log", new=AsyncMock(side_effect=RuntimeError("audit down"))):
            token = await auth.authenticate("alice", plain, "1.1.1.1")

        assert isinstance(token, str) and len(token) > 30
        # A session row + a TRUE attempt were both inserted
        assert any("companion_auth_sessions" in s for (s, _) in conn.executed)
        assert any("companion_login_attempts" in s and "TRUE" in s
                   for (s, _) in conn.executed)


# ═══════════════════════════════════════════════════════════════════════════
# get_user_from_token — rolling refresh + refresh-error swallow (215-216)
# ═══════════════════════════════════════════════════════════════════════════
class TestGetUserFromTokenRefresh:
    @pytest.mark.asyncio
    async def test_near_expiry_triggers_refresh_update(self):
        """A valid token expiring in <3 days fires the rolling-refresh UPDATE
        and still returns the user id."""
        soon = datetime.now(timezone.utc) + timedelta(days=1)  # < 3 days
        ctx, conn = _make_conn(fetchone_return=("alice", soon))
        with patch("app.auth.get_conn_autocommit", return_value=ctx):
            user = await auth.get_user_from_token("tok")

        assert user == "alice"
        assert any("SET expires_at = NOW() + INTERVAL '7 days'" in s
                   for (s, _) in conn.executed), "refresh UPDATE should run"

    @pytest.mark.asyncio
    async def test_far_expiry_does_not_refresh(self):
        """A token with >3 days left returns the user WITHOUT a refresh UPDATE."""
        far = datetime.now(timezone.utc) + timedelta(days=30)
        ctx, conn = _make_conn(fetchone_return=("bob", far))
        with patch("app.auth.get_conn_autocommit", return_value=ctx):
            user = await auth.get_user_from_token("tok")

        assert user == "bob"
        assert not any("SET expires_at" in s for (s, _) in conn.executed)

    @pytest.mark.asyncio
    async def test_refresh_update_failure_is_swallowed(self):
        """If the refresh UPDATE raises, the user id is STILL returned (the
        refresh is best-effort, lines 215-216)."""
        soon = datetime.now(timezone.utc) + timedelta(days=1)
        conn = MagicMock()
        calls = {"n": 0}

        async def _execute(sql, params=None):
            calls["n"] += 1
            if "SET expires_at" in sql:
                raise RuntimeError("update failed")
            res = MagicMock()
            res.fetchone = AsyncMock(return_value=("carol", soon))
            return res

        conn.execute = _execute
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=None)

        with patch("app.auth.get_conn_autocommit", return_value=ctx):
            user = await auth.get_user_from_token("tok")

        assert user == "carol", "refresh failure must not deny an active session"


# ═══════════════════════════════════════════════════════════════════════════
# get_session_info — empty-row branch (line 238)
# ═══════════════════════════════════════════════════════════════════════════
class TestGetSessionInfo:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_row(self):
        ctx, _ = _make_conn(fetchone_return=None)
        with patch("app.auth.get_conn", return_value=ctx):
            info = await auth.get_session_info("bogus")
        assert info is None

    @pytest.mark.asyncio
    async def test_returns_isoformat_metadata(self):
        """Populated row → user_id + ISO-8601 created_at / expires_at."""
        created = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        expires = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
        ctx, _ = _make_conn(fetchone_return=("alice", created, expires))
        with patch("app.auth.get_conn", return_value=ctx):
            info = await auth.get_session_info("good-token")

        assert info == {
            "user_id": "alice",
            "created_at": created.isoformat(),
            "expires_at": expires.isoformat(),
        }

    @pytest.mark.asyncio
    async def test_handles_null_timestamps(self):
        """NULL created_at / expires_at → None values, no crash (the
        `if ... else None` conditional branches)."""
        ctx, _ = _make_conn(fetchone_return=("alice", None, None))
        with patch("app.auth.get_conn", return_value=ctx):
            info = await auth.get_session_info("good-token")
        assert info == {"user_id": "alice", "created_at": None, "expires_at": None}


# ═══════════════════════════════════════════════════════════════════════════
# change_password — audit-log swallow on success (lines 279-280)
# ═══════════════════════════════════════════════════════════════════════════
class TestChangePasswordAuditSwallow:
    @pytest.mark.asyncio
    async def test_success_invalidates_sessions_and_swallows_audit_error(self):
        """Correct old password: hash is updated, ALL sessions deleted, and a
        failing audit.log is swallowed while still returning True."""
        old_plain = "old-pw-12345"
        old_hash = bcrypt.hashpw(old_plain.encode(), bcrypt.gensalt()).decode()
        ctx, conn = _make_conn(fetchone_return=(old_hash,))
        with patch("app.auth.get_conn_autocommit", return_value=ctx), \
             patch("app.audit.log", new=AsyncMock(side_effect=RuntimeError("audit down"))):
            ok = await auth.change_password("alice", old_plain, "brand-new-pw-9999")

        assert ok is True
        # New hash written + all sessions invalidated
        updates = [(s, p) for (s, p) in conn.executed if "UPDATE companion_users" in s]
        assert len(updates) == 1
        new_hash = updates[0][1][0]
        assert bcrypt.checkpw(b"brand-new-pw-9999", new_hash.encode())
        assert any("DELETE FROM companion_auth_sessions" in s for (s, _) in conn.executed)

    @pytest.mark.asyncio
    async def test_wrong_old_password_makes_no_changes(self):
        """SECURITY: a wrong current password must NOT update the hash or
        delete sessions, and must return False."""
        old_hash = bcrypt.hashpw(b"the-real-old", bcrypt.gensalt()).decode()
        ctx, conn = _make_conn(fetchone_return=(old_hash,))
        with patch("app.auth.get_conn_autocommit", return_value=ctx):
            ok = await auth.change_password("alice", "guessed-wrong", "newpw-123456")

        assert ok is False
        assert not any("UPDATE companion_users" in s for (s, _) in conn.executed)
        assert not any("DELETE FROM companion_auth_sessions" in s for (s, _) in conn.executed)

    @pytest.mark.asyncio
    async def test_unknown_user_returns_false(self):
        """No user row → False, no writes."""
        ctx, conn = _make_conn(fetchone_return=None)
        with patch("app.auth.get_conn_autocommit", return_value=ctx):
            ok = await auth.change_password("ghost", "whatever", "newpw-123456")
        assert ok is False
        assert not any("UPDATE companion_users" in s for (s, _) in conn.executed)
