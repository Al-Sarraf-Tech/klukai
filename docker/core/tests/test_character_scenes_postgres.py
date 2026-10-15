"""Opt-in PostgreSQL proof for the once-ever oath transaction."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from psycopg.pq import TransactionStatus

from app.character_scenes import commit_oath_scene
from app.migrations import apply_migrations
from tests.test_migrations_postgres import MIGRATIONS, migration_pool, rows  # noqa: F401


@pytest.fixture
async def oath_db(migration_pool, monkeypatch):  # noqa: F811
    await apply_migrations(migration_pool, MIGRATIONS)

    @asynccontextmanager
    async def connection():
        conn = await migration_pool.getconn()
        try:
            yield conn
        finally:
            if (
                not conn.closed
                and conn.info.transaction_status != TransactionStatus.IDLE
            ):
                await conn.rollback()
            await migration_pool.putconn(conn)

    monkeypatch.setattr("app.character_scenes.get_conn", connection)
    return migration_pool


async def _counts(pool):
    firsts = await rows(pool, "SELECT count(*) FROM companion_firsts")
    messages = await rows(pool, "SELECT count(*) FROM companion_messages")
    return firsts[0][0], messages[0][0]


async def test_concurrent_callers_leave_one_complete_scene(oath_db):
    scene = ["Stay.", "I chose you."]
    results = await asyncio.gather(
        commit_oath_scene("u1", scene),
        commit_oath_scene("u1", scene),
    )
    assert sorted(results) == [False, True]
    assert await _counts(oath_db) == (1, 2)
    stored = await rows(
        oath_db,
        "SELECT content FROM companion_messages ORDER BY created_at",
    )
    assert [row[0] for row in stored] == scene
    again = await commit_oath_scene("u1", scene)
    assert again is False
    assert await _counts(oath_db) == (1, 2)


async def test_failure_after_the_claim_rolls_the_trigger_back(oath_db):
    async with oath_db.connection() as conn:
        await conn.execute(
            """
            CREATE FUNCTION fail_second_oath_line() RETURNS trigger AS $$
            BEGIN
                IF (SELECT count(*) FROM companion_messages) >= 1 THEN
                    RAISE EXCEPTION 'stop after the first line';
                END IF;
                RETURN NEW;
            END $$ LANGUAGE plpgsql
            """
        )
        await conn.execute(
            """
            CREATE TRIGGER fail_second_oath_line
            BEFORE INSERT ON companion_messages
            FOR EACH ROW EXECUTE FUNCTION fail_second_oath_line()
            """
        )
    assert await commit_oath_scene("u1", ["Stay.", "I chose you."]) is False
    assert await _counts(oath_db) == (0, 0)
    async with oath_db.connection() as conn:
        await conn.execute("DROP TRIGGER fail_second_oath_line ON companion_messages")
    assert await commit_oath_scene("u1", ["Stay.", "I chose you."]) is True
    assert await _counts(oath_db) == (1, 2)


async def test_existing_trigger_is_not_replayed_or_reset(oath_db):
    async with oath_db.connection() as conn:
        await conn.execute(
            "INSERT INTO companion_firsts (user_id, event_type, event_date) "
            "VALUES (%s, %s, CURRENT_DATE)",
            ("u1", "oath_fulfilled"),
        )
        stored = await (await conn.execute(
            "SELECT id, event_type FROM companion_firsts WHERE user_id = %s",
            ("u1",),
        )).fetchone()
    assert await commit_oath_scene("u1", ["Stay.", "I chose you."]) is False
    assert await _counts(oath_db) == (1, 0)
    async with oath_db.connection() as conn:
        again = await (await conn.execute(
            "SELECT id, event_type FROM companion_firsts WHERE user_id = %s",
            ("u1",),
        )).fetchone()
    assert again == stored
