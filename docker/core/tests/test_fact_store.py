"""app.fact_store against a recording fake connection (no database).

The real-PostgreSQL behaviour is proven in tests/runtime_backends.py
(test_postgres_fact_memory_keeps_the_data_service_contract); these cover the
module's own control flow: first-use schema, input checks, TTL handling,
atomic insert, recall shapes and the batched purge.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from app import fact_store

TS = datetime(2026, 10, 27, tzinfo=timezone.utc)


class Cursor:
    def __init__(self, rows=(), rowcount=0):
        self.rows = list(rows)
        self.rowcount = rowcount

    async def fetchone(self):
        return self.rows[0] if self.rows else None

    async def fetchall(self):
        return self.rows


class Conn:
    """Answers queries from a script of (substring, Cursor) pairs, in order."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        for i, (needle, cursor) in enumerate(self.script):
            if needle in sql:
                return self.script.pop(i)[1]
        return Cursor()


@pytest.fixture
def db(monkeypatch):
    holder = {"conn": Conn([])}

    @asynccontextmanager
    async def connect():
        yield holder["conn"]

    monkeypatch.setattr(fact_store, "get_conn", connect)
    monkeypatch.setattr(fact_store, "get_conn_autocommit", connect)
    monkeypatch.setattr(fact_store, "_ready", True)

    def use(*script):
        holder["conn"] = Conn(script)
        return holder["conn"]

    return use


class TestSchema:
    async def test_existing_table_runs_no_ddl(self, db, monkeypatch):
        monkeypatch.setattr(fact_store, "_ready", False)
        conn = db(("to_regclass", Cursor([(True,)])))
        await fact_store._ensure_table()
        assert not any("CREATE" in sql for sql, _ in conn.calls)
        assert "pg_advisory_xact_lock" in conn.calls[0][0]
        assert fact_store._ready is True

    async def test_missing_table_is_created_with_its_indexes(self, db, monkeypatch):
        monkeypatch.setattr(fact_store, "_ready", False)
        conn = db(("to_regclass", Cursor([(False,)])))
        await fact_store._ensure_table()
        created = [sql for sql, _ in conn.calls if sql.startswith("CREATE")]
        assert created == list(fact_store._SCHEMA)

    async def test_checked_once_per_process(self, db):
        conn = db()
        await fact_store._ensure_table()
        assert conn.calls == []


class TestStore:
    @pytest.mark.parametrize("key", ["", "   "])
    async def test_empty_key_is_refused(self, db, key):
        with pytest.raises(ValueError):
            await fact_store.store(key, "v")
        with pytest.raises(ValueError):
            await fact_store.insert_if_absent(key, "v")

    async def test_positive_ttl_sets_an_expiry(self, db):
        conn = db()
        await fact_store.store(" k ", "v", 90)
        sql, params = conn.calls[0]
        assert "make_interval(secs => %s)" in sql and params == ("k", "v", 90, 90)

    @pytest.mark.parametrize("ttl", [None, 0, -5])
    async def test_no_positive_ttl_makes_it_permanent(self, db, ttl):
        conn = db()
        await fact_store.store("k", "v", ttl)
        sql, params = conn.calls[0]
        assert "expires_at=NULL" in sql and params == ("k", "v")

    async def test_insert_if_absent_reports_whether_it_stored(self, db):
        db(("ON CONFLICT(key) DO NOTHING", Cursor([("k",)])))
        assert await fact_store.insert_if_absent("k", "v") is True
        db(("ON CONFLICT(key) DO NOTHING", Cursor([])))
        assert await fact_store.insert_if_absent("k", "v") is False


class TestRecall:
    async def test_key_found_and_missing(self, db):
        db(("WHERE key=%s", Cursor([("k", "v", TS)])))
        assert await fact_store.recall("k") == {"key": "k", "value": "v", "ts": str(TS)}
        db(("WHERE key=%s", Cursor([])))
        assert await fact_store.recall("k") is None

    async def test_pattern_is_bounded(self, db):
        conn = db(("key LIKE %s", Cursor([("a", "1", TS), ("b", "2", TS)])))
        rows = await fact_store.recall_pattern("companion:u:%")
        assert [r["key"] for r in rows] == ["a", "b"]
        assert conn.calls[0][1] == ("companion:u:%", fact_store.PATTERN_LIMIT)


class TestPurge:
    async def test_stops_after_a_partial_batch(self, db):
        conn = db(
            ("DELETE FROM memory", Cursor(rowcount=3)),
            ("DELETE FROM memory", Cursor(rowcount=1)),
        )
        assert await fact_store.purge_expired(batch=3) == 4
        assert len(conn.calls) == 2
        assert conn.calls[0][1] == (3600, 3, 3600)

    async def test_bounded_number_of_batches(self, db):
        conn = db(*[("DELETE FROM memory", Cursor(rowcount=2)) for _ in range(5)])
        assert await fact_store.purge_expired(batch=2, max_batches=3) == 6
        assert len(conn.calls) == 3

    @pytest.mark.parametrize("rowcount", [0, -1, None])
    async def test_unknown_or_zero_rowcount_counts_as_none(self, db, rowcount):
        db(("DELETE FROM memory", Cursor(rowcount=rowcount)))
        assert await fact_store.purge_expired() == 0

    async def test_negative_margin_is_clamped(self, db):
        conn = db(("DELETE FROM memory", Cursor(rowcount=0)))
        await fact_store.purge_expired(margin_seconds=-10)
        assert conn.calls[0][1][0] == 0


def test_escape_like_makes_wildcards_literal():
    assert fact_store.escape_like("a_b%c\\d") == "a\\_b\\%c\\\\d"
