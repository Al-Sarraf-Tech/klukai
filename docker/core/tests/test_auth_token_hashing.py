"""Tests for session-token hashing-at-rest, absolute lifetime, and voice auth.

These lock in the audit-3 hardening: tokens are stored as a sha256 hash (the
plaintext is only the client-side bearer), a session can't be rolled forward
past SESSION_MAX_DAYS, and core->voice calls carry a bearer only when configured.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app import auth
from app.helpers import voice_auth_headers


def test_voice_auth_headers_empty_without_token(monkeypatch):
    monkeypatch.delenv("VOICE_API_TOKEN", raising=False)
    assert voice_auth_headers() == {}


def test_voice_auth_headers_bearer_with_token(monkeypatch):
    monkeypatch.setenv("VOICE_API_TOKEN", "vt-secret")
    assert voice_auth_headers() == {"Authorization": "Bearer vt-secret"}


def test_hash_token_is_deterministic_sha256_hex():
    h = auth._hash_token("a-token")
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    assert h == auth._hash_token("a-token")
    assert h != auth._hash_token("a-token-2")


def _conn_ctx(conn):
    @asynccontextmanager
    async def _ctx():
        yield conn
    return _ctx


class _Cur:
    def __init__(self, ret):
        self._ret = ret

    async def fetchone(self):
        return self._ret


class TestTokenStoredHashed:
    @pytest.mark.asyncio
    async def test_authenticate_stores_hash_not_plaintext(self, monkeypatch):
        import bcrypt

        pw = "the-right-pw"
        pw_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
        executed: list = []

        class _Conn:
            async def execute(self, sql, params=None):
                executed.append((sql, params))
                if sql.startswith("SELECT id, password_hash"):
                    return _Cur(("alice", pw_hash))
                return _Cur(None)

        monkeypatch.setattr(auth, "get_conn_autocommit", _conn_ctx(_Conn()))
        from unittest.mock import AsyncMock
        monkeypatch.setattr("app.audit.log", AsyncMock())

        token = await auth.authenticate("alice", pw, "1.2.3.4")
        assert token  # plaintext bearer is returned to the client

        inserts = [p for s, p in executed if s.startswith("INSERT INTO companion_auth_sessions")]
        assert inserts, "expected a session INSERT"
        stored = inserts[0][0]
        assert stored == auth._hash_token(token)   # DB holds the hash
        assert stored != token                       # never the plaintext


class TestAbsoluteLifetime:
    def _conn_returning(self, row):
        class _Conn:
            async def execute(self, sql, params=None):
                return _Cur(row)
        return _Conn()

    @pytest.mark.asyncio
    async def test_token_past_absolute_lifetime_rejected(self, monkeypatch):
        now = datetime.now(timezone.utc)
        row = ("alice", now + timedelta(days=5), now - timedelta(days=auth.SESSION_MAX_DAYS + 1))
        monkeypatch.setattr(auth, "get_conn_autocommit", _conn_ctx(self._conn_returning(row)))
        # Valid expires_at, but created beyond the absolute cap → rejected.
        assert await auth.get_user_from_token("tok") is None

    @pytest.mark.asyncio
    async def test_fresh_token_within_lifetime_accepted(self, monkeypatch):
        now = datetime.now(timezone.utc)
        row = ("alice", now + timedelta(days=5), now - timedelta(days=1))
        monkeypatch.setattr(auth, "get_conn_autocommit", _conn_ctx(self._conn_returning(row)))
        assert await auth.get_user_from_token("tok") == "alice"
