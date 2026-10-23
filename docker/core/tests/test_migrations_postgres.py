"""Opt-in real PostgreSQL proof; never reads the production DATABASE_URL.

Each test gets a random schema and drops only that schema on teardown.
Point KLUKAI_MIGRATION_TEST_DATABASE_URL at a disposable PostgreSQL database.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg_pool import AsyncConnectionPool
import pytest

from app.migrations import apply_migrations

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


@pytest.fixture
async def migration_pool():
    url = os.environ.get("KLUKAI_MIGRATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set KLUKAI_MIGRATION_TEST_DATABASE_URL to a disposable PostgreSQL database")
    schema = "migration_test_" + uuid.uuid4().hex
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            async with AsyncConnectionPool(
                url, min_size=1, max_size=4, open=False,
                kwargs={"options": f"-c search_path={schema}"},
            ) as pool:
                await pool.wait()
                yield pool
        finally:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


async def rows(pool, statement):
    async with pool.connection() as conn:
        return await (await conn.execute(statement)).fetchall()


async def legacy_schema(pool, before=None):
    """Build the historical no-ledger schema by executing the shipped scripts."""
    async with pool.connection() as conn:
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if before and path.name >= before:
                break
            await conn.execute(path.read_text())


async def test_fresh_schema_then_repeat_changes_nothing(migration_pool):
    applied = await apply_migrations(migration_pool, MIGRATIONS)
    assert len(applied) == 26
    assert await rows(migration_pool, "SELECT count(*) FROM companion_gifts") == [(1,)]
    assert await rows(migration_pool, "SELECT count(*) FROM companion_schema_migrations") == [(26,)]
    assert await rows(
        migration_pool,
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'companion_memories' "
        "AND column_name = 'annotation_history'",
    ) == [("jsonb", "NO")]
    assert await apply_migrations(migration_pool, MIGRATIONS) == []
    assert await rows(migration_pool, "SELECT count(*) FROM companion_gifts") == [(1,)]


async def test_legacy_pre030_score_is_scaled_once(migration_pool):
    await legacy_schema(migration_pool, before="030")
    async with migration_pool.connection() as conn:
        await conn.execute("UPDATE companion_affection SET score = 5")
    await apply_migrations(migration_pool, MIGRATIONS)
    assert await rows(migration_pool, "SELECT score FROM companion_affection") == [(50,)]
    await apply_migrations(migration_pool, MIGRATIONS)
    assert await rows(migration_pool, "SELECT score FROM companion_affection") == [(50,)]


async def test_current_legacy_preserves_low_score_duplicates_and_history(migration_pool):
    await legacy_schema(migration_pool)
    async with migration_pool.connection() as conn:
        await conn.execute("UPDATE companion_affection SET score = 5")
        await conn.execute("INSERT INTO companion_gifts (user_id,item) VALUES ('jalsarraf','Klukadile plush')")
        await conn.execute("INSERT INTO companion_conversations(id) VALUES ('00000000-0000-0000-0000-000000000001')")
        await conn.execute("INSERT INTO companion_messages(conversation_id,role,content) VALUES ('00000000-0000-0000-0000-000000000001','user','keep this history')")
    gifts_before = await rows(migration_pool, "SELECT * FROM companion_gifts ORDER BY id")
    history_before = await rows(migration_pool, "SELECT * FROM companion_messages ORDER BY id")
    assert len(gifts_before) == 2
    await apply_migrations(migration_pool, MIGRATIONS)
    await apply_migrations(migration_pool, MIGRATIONS)
    assert await rows(migration_pool, "SELECT score FROM companion_affection") == [(5,)]
    assert await rows(migration_pool, "SELECT * FROM companion_gifts ORDER BY id") == gifts_before
    assert await rows(migration_pool, "SELECT * FROM companion_messages ORDER BY id") == history_before


async def test_lexical_order_exact_bytes_and_once_only(migration_pool, tmp_path):
    (tmp_path / "020.sql").write_text("INSERT INTO proof VALUES (2);")
    first = tmp_path / "010.sql"
    first.write_bytes(b"CREATE TABLE proof (n int);\r\nINSERT INTO proof VALUES (1); -- \xc3\xa9\r\n")
    assert await apply_migrations(migration_pool, tmp_path) == ["010.sql", "020.sql"]
    assert await apply_migrations(migration_pool, tmp_path) == []
    assert await rows(migration_pool, "SELECT * FROM proof ORDER BY n") == [(1,), (2,)]
    assert await rows(migration_pool, "SELECT sha256 FROM companion_schema_migrations WHERE name='010.sql'") == [(hashlib.sha256(first.read_bytes()).hexdigest(),)]


@pytest.mark.parametrize("mutation", ["changed", "removed"])
async def test_changed_or_missing_applied_file_fails_before_pending_sql(migration_pool, tmp_path, mutation):
    applied = tmp_path / "010.sql"
    applied.write_text("CREATE TABLE proof(n int);")
    await apply_migrations(migration_pool, tmp_path)
    (tmp_path / "001_pending.sql").write_text("INSERT INTO proof VALUES(99);")
    if mutation == "changed":
        applied.write_text("-- edited\nCREATE TABLE proof(n int);")
    else:
        applied.unlink()
    with pytest.raises(RuntimeError, match="010.sql"):
        await apply_migrations(migration_pool, tmp_path)
    assert await rows(migration_pool, "SELECT * FROM proof") == []
    assert await rows(migration_pool, "SELECT name FROM companion_schema_migrations") == [("010.sql",)]


async def test_sql_failure_rolls_back_all_pending_files_and_ledger(migration_pool, tmp_path):
    (tmp_path / "001.sql").write_text("CREATE TABLE retained(n int);")
    await apply_migrations(migration_pool, tmp_path)
    (tmp_path / "002.sql").write_text("CREATE TABLE rolled_back(n int); INSERT INTO retained VALUES(1);")
    broken = tmp_path / "003.sql"
    broken.write_text("INSERT INTO retained VALUES(2); SELECT absent_column FROM retained;")
    with pytest.raises(psycopg.Error):
        await apply_migrations(migration_pool, tmp_path)
    assert await rows(migration_pool, "SELECT * FROM retained") == []
    assert await rows(migration_pool, "SELECT to_regclass('rolled_back')") == [(None,)]
    assert await rows(migration_pool, "SELECT name FROM companion_schema_migrations") == [("001.sql",)]
    broken.write_text("INSERT INTO retained VALUES(2);")
    assert await apply_migrations(migration_pool, tmp_path) == ["002.sql", "003.sql"]
    assert await rows(migration_pool, "SELECT * FROM retained ORDER BY n") == [(1,), (2,)]


async def test_concurrent_runners_commit_each_migration_once(migration_pool, tmp_path):
    (tmp_path / "001.sql").write_text("SELECT pg_sleep(0.2); CREATE TABLE proof(n int); INSERT INTO proof VALUES(1);")
    outcomes = await asyncio.gather(*(apply_migrations(migration_pool, tmp_path) for _ in range(3)))
    assert sorted(outcomes) == [[], [], ["001.sql"]]
    assert await rows(migration_pool, "SELECT * FROM proof") == [(1,)]
    assert await rows(migration_pool, "SELECT count(*) FROM companion_schema_migrations") == [(1,)]


async def test_initial_failure_leaves_no_ledger_or_schema_changes(migration_pool, tmp_path):
    (tmp_path / "001.sql").write_text("CREATE TABLE proof(n int); SELECT absent_column FROM proof;")
    with pytest.raises(psycopg.Error):
        await apply_migrations(migration_pool, tmp_path)
    assert await rows(migration_pool, "SELECT to_regclass('proof'), to_regclass('companion_schema_migrations')") == [(None, None)]


async def test_advisory_lock_wait_is_bounded_and_connection_recovers(migration_pool, tmp_path):
    (tmp_path / "001.sql").write_text("CREATE TABLE proof(n int);")
    async with migration_pool.connection() as holder:
        await holder.execute("SELECT pg_advisory_xact_lock(5425801856799755591)")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            await asyncio.wait_for(apply_migrations(migration_pool, tmp_path), timeout=35)
        assert await rows(migration_pool, "SELECT to_regclass('companion_schema_migrations')") == [(None,)]
    assert await apply_migrations(migration_pool, tmp_path) == ["001.sql"]


async def test_ledger_write_failure_rolls_back_migration_effects(migration_pool, tmp_path):
    (tmp_path / "001.sql").write_text("CREATE TABLE proof(n int);")
    await apply_migrations(migration_pool, tmp_path)
    async with migration_pool.connection() as conn:
        await conn.execute("""
            CREATE FUNCTION reject_migration_record() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'fixture rejects ledger insert';
            END $$;
            CREATE TRIGGER reject_migration_record BEFORE INSERT ON companion_schema_migrations
            FOR EACH ROW EXECUTE FUNCTION reject_migration_record();
        """)
    (tmp_path / "002.sql").write_text("INSERT INTO proof VALUES(99);")
    with pytest.raises(psycopg.errors.RaiseException, match="fixture rejects"):
        await apply_migrations(migration_pool, tmp_path)
    assert await rows(migration_pool, "SELECT * FROM proof") == []
    assert await rows(migration_pool, "SELECT name FROM companion_schema_migrations") == [("001.sql",)]


async def test_seed_existence_is_scoped_to_user_and_item(migration_pool):
    await legacy_schema(migration_pool)
    async with migration_pool.connection() as conn:
        await conn.execute("UPDATE companion_gifts SET user_id='other_commander'")
        await conn.execute("INSERT INTO companion_gifts(user_id,item) VALUES('jalsarraf','different gift')")
    await apply_migrations(migration_pool, MIGRATIONS)
    assert await rows(migration_pool, "SELECT user_id,item FROM companion_gifts ORDER BY id") == [
        ("other_commander", "Klukadile plush"),
        ("jalsarraf", "different gift"),
        ("jalsarraf", "Klukadile plush"),
    ]


async def test_reannotation_archives_the_annotation_the_update_replaces(migration_pool):
    """Successive rewrites each archive the text they replace, so history is
    the full chain and nothing is lost, even when the second writer read the
    row before the first one committed."""
    import reannotate_existing

    await apply_migrations(migration_pool, MIGRATIONS)
    async with migration_pool.connection() as conn:
        mem_id = (
            await (
                await conn.execute(
                    "INSERT INTO companion_memories (filename, annotation, user_id, conversation_id) "
                    "VALUES ('m.webp', 'original', 'claude', 'seed:x') RETURNING id::text"
                )
            ).fetchone()
        )[0]
        await conn.commit()
        assert await reannotate_existing._replace_annotation(conn, mem_id, "claude", "first")
        assert await reannotate_existing._replace_annotation(conn, mem_id, "claude", "second")
    rows_ = await rows(
        migration_pool,
        "SELECT annotation, jsonb_path_query_array(annotation_history, '$[*].annotation') "
        "FROM companion_memories",
    )
    assert rows_ == [("second", ["original", "first"])]
