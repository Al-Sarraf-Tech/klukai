"""Real transaction interleavings: old credentials cannot survive revocation."""

import asyncio
import os
import threading
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import bcrypt
import psycopg
from psycopg import sql
from psycopg_pool import AsyncConnectionPool
import pytest

from app import auth, db


@pytest.fixture
async def auth_pool(monkeypatch):
    url = os.getenv("KLUKAI_MIGRATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires the explicit disposable PostgreSQL test URL")
    schema = "auth_test_" + uuid.uuid4().hex
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            async with AsyncConnectionPool(
                url,
                min_size=2,
                max_size=6,
                open=False,
                kwargs={"options": f"-c search_path={schema}"},
            ) as pool:
                await pool.wait()
                async with pool.connection() as conn:
                    await conn.execute(
                        "CREATE TABLE companion_users(id text PRIMARY KEY, username text, password_hash text, deactivated_at timestamptz)"
                    )
                    await conn.execute(
                        "CREATE TABLE companion_auth_sessions(token text PRIMARY KEY, user_id text, expires_at timestamptz DEFAULT now()+interval '7 days')"
                    )
                    await conn.execute(
                        "CREATE TABLE companion_login_attempts(ip_address text,success boolean)"
                    )
                    await conn.execute(
                        "INSERT INTO companion_users VALUES('alice','alice',%s,NULL)",
                        (
                            bcrypt.hashpw(
                                b"original-password", bcrypt.gensalt(rounds=4)
                            ).decode(),
                        ),
                    )
                monkeypatch.setattr(db, "_pool", pool)
                monkeypatch.setattr("app.audit.log", AsyncMock())
                monkeypatch.setattr("app.context.ws.revoke_user", AsyncMock())
                yield pool
        finally:
            await admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def pause_read(monkeypatch, prefix, parties=1):
    original = auth.get_conn_autocommit
    read = asyncio.Event()
    release = asyncio.Event()
    count = 0

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        async def fetchone(self):
            nonlocal count
            row = await self.cursor.fetchone()
            count += 1
            if count <= parties:
                if count == parties:
                    read.set()
                await release.wait()
            return row

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, key):
            return getattr(self.conn, key)

        async def execute(self, query, params=None):
            cursor = await self.conn.execute(query, params)
            return Cursor(cursor) if str(query).startswith(prefix) else cursor

    @asynccontextmanager
    async def wrapped():
        async with original() as conn:
            yield Connection(conn)

    monkeypatch.setattr(auth, "get_conn_autocommit", wrapped)
    return read, release


async def test_password_verification_does_not_block_event_loop(auth_pool, monkeypatch):
    threads = []
    check = bcrypt.checkpw

    def observed(password, hashed):
        threads.append(threading.get_ident())
        return check(password, hashed)

    monkeypatch.setattr(bcrypt, "checkpw", observed)
    assert await auth.authenticate("alice", "original-password", "127.0.0.1")
    assert threads and all(t != threading.get_ident() for t in threads)


@pytest.mark.parametrize("revocation", ["password", "deactivation"])
async def test_login_rechecks_credentials_after_concurrent_revocation(
    auth_pool, monkeypatch, revocation
):
    read, release = pause_read(monkeypatch, "SELECT id, password_hash")
    login = asyncio.create_task(
        auth.authenticate("alice", "original-password", "127.0.0.1")
    )
    try:
        await asyncio.wait_for(read.wait(), 3)
        if revocation == "password":
            assert await auth.change_password(
                "alice", "original-password", "replacement-password"
            )
        else:
            async with auth_pool.connection() as conn:
                await conn.execute(
                    "UPDATE companion_users SET deactivated_at=now() WHERE id='alice'"
                )
                await conn.execute(
                    "DELETE FROM companion_auth_sessions WHERE user_id='alice'"
                )
        release.set()
        assert await asyncio.wait_for(login, 3) is None
        async with auth_pool.connection() as conn:
            assert await (
                await conn.execute("SELECT count(*) FROM companion_auth_sessions")
            ).fetchone() == (0,)
    finally:
        release.set()
        await asyncio.gather(login, return_exceptions=True)


async def test_only_one_concurrent_password_change_can_use_the_old_password(
    auth_pool, monkeypatch
):
    read, release = pause_read(monkeypatch, "SELECT password_hash", parties=2)
    tasks = [
        asyncio.create_task(
            auth.change_password(
                "alice", "original-password", f"replacement-password-{i}"
            )
        )
        for i in range(2)
    ]
    try:
        await asyncio.wait_for(read.wait(), 3)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert sorted(results) == [False, True]
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_oversized_utf8_password_counts_as_a_failed_login(auth_pool):
    assert await auth.authenticate("alice", "é" * 37, "127.0.0.1") is None
    async with auth_pool.connection() as conn:
        assert await (
            await conn.execute(
                "SELECT count(*) FROM companion_login_attempts WHERE success=false"
            )
        ).fetchone() == (1,)
