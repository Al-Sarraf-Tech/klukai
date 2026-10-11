"""reannotate_existing.py: exact-fingerprint matching, user scoping, dry-run by
default, and lossless rewrites (old annotation kept in annotation_history)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import reannotate_existing as reann

TS = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
FINGERPRINT = f"seed:{TS.isoformat()}"
OLD = "Whisper of a moment, souls entwined under moonlit sheets."
NEW = (
    "He sat beside me at 0300 in the office with cold coffee and said nothing, "
    "and I let my shoulder rest against his. I did not pull away."
)


def _match(conversation_id, exchanges):
    return reann._match_exchange(conversation_id, exchanges)


def test_match_exchange_is_exact_fingerprint_only() -> None:
    exchanges = {FINGERPRINT: {"user": "u", "assistant": "a", "created_at": TS.isoformat()}}
    assert _match(FINGERPRINT, exchanges) is exchanges[FINGERPRINT]
    assert _match("seed", exchanges) is None  # legacy row: no fingerprint
    assert _match("seed:2025-01-01T00:00:00+00:00", exchanges) is None
    assert _match(None, exchanges) is None
    assert _match("", exchanges) is None
    # Text overlap, however high, never matches anything.
    assert _match("her_pov:" + TS.isoformat(), exchanges) is None


class _Db:
    """Dispatch conn.execute by SQL text and record every write."""

    def __init__(self, memories: list[tuple], messages: list[tuple], fail_update: bool = False):
        self.memories = memories
        self.messages = messages
        self.fail_update = fail_update
        self.selects: list[tuple[str, tuple]] = []
        self.writes: list[tuple[str, tuple]] = []
        self.conn = MagicMock()
        self.conn.execute = AsyncMock(side_effect=self._execute)
        self.conn.commit = AsyncMock()
        self.conn.rollback = AsyncMock()
        self.conn.close = AsyncMock()

    async def _execute(self, sql: str, params: tuple = ()) -> MagicMock:
        cursor = MagicMock()
        if sql.lstrip().startswith("SELECT"):
            self.selects.append((sql, params))
            if "FROM companion_memories" in sql:
                cursor.fetchall = AsyncMock(return_value=self.memories)
            elif "FROM companion_messages" in sql:
                cursor.fetchall = AsyncMock(return_value=self.messages)
            else:  # companion_relationship progress marker
                cursor.fetchone = AsyncMock(return_value=None)
            return cursor
        self.writes.append((sql, params))
        if self.fail_update and sql.lstrip().startswith("UPDATE"):
            raise RuntimeError("write refused")
        return cursor


def _wire(monkeypatch: pytest.MonkeyPatch, db: _Db, annotation: str | None = NEW) -> AsyncMock:
    monkeypatch.setattr(reann, "DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(
        reann.psycopg.AsyncConnection, "connect", AsyncMock(return_value=db.conn)
    )
    annotator = AsyncMock(return_value=annotation)
    monkeypatch.setattr(reann, "_call_annotator", annotator)
    monkeypatch.setattr(reann.asyncio, "sleep", AsyncMock())
    return annotator


def _messages() -> list[tuple]:
    return [("user", "Stay a while?", TS), ("assistant", "...Fine. Just this once.", TS)]


@pytest.mark.asyncio
async def test_default_is_dry_run_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _Db([("m1", OLD, "Quiet Hours", FINGERPRINT)], _messages())
    annotator = _wire(monkeypatch, db)

    assert await reann.main(["--user", "claude"]) == 0

    annotator.assert_awaited_once()
    assert db.writes == []
    db.conn.commit.assert_not_awaited()
    db.conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_loads_are_user_scoped_and_legacy_rows_are_skipped_not_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _Db(
        [("legacy", OLD, "Quiet Hours", "seed"), ("m1", OLD, "Quiet Hours", FINGERPRINT)],
        _messages(),
    )
    annotator = _wire(monkeypatch, db)

    assert await reann.main(["--user", "claude"]) == 0

    mem_sql, mem_params = next(s for s in db.selects if "FROM companion_memories" in s[0])
    assert "user_id = %s AND conversation_id LIKE 'seed%%'" in mem_sql
    assert mem_params == ("claude",)
    msg_sql, msg_params = next(s for s in db.selects if "FROM companion_messages" in s[0])
    assert "WHERE user_id = %s" in msg_sql
    assert msg_params == ("claude",)
    # Only the fingerprinted memory reached the annotator; the legacy one was
    # counted and skipped, never matched by text similarity.
    annotator.assert_awaited_once()
    assert annotator.await_args.args[1:] == ("Stay a while?", "...Fine. Just this once.", "Quiet Hours")


@pytest.mark.asyncio
async def test_rewrite_keeps_old_annotation_in_history_and_scopes_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _Db([("m1", OLD, "Quiet Hours", FINGERPRINT)], _messages())
    _wire(monkeypatch, db)

    assert await reann.main(["--user", "claude", "--rewrite"]) == 0

    updates = [w for w in db.writes if w[0].lstrip().startswith("UPDATE")]
    assert len(updates) == 1
    sql, params = updates[0]
    assert "annotation_history = annotation_history || %s::jsonb" in sql
    assert "WHERE id = %s AND user_id = %s" in sql
    new_annotation, history_json, mem_id, user_id = params
    assert new_annotation == NEW
    assert (mem_id, user_id) == ("m1", "claude")
    history = json.loads(history_json)
    assert history[0]["annotation"] == OLD
    datetime.fromisoformat(history[0]["replaced_at"])  # valid ISO timestamp
    # Progress checkpoint lands only in rewrite mode.
    inserts = [w for w in db.writes if "reannotated_ids" in w[0]]
    assert len(inserts) == 1
    assert json.loads(inserts[0][1][0]) == ["m1"]
    assert db.conn.commit.await_count >= 2


@pytest.mark.asyncio
async def test_rewrite_failure_rolls_back_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _Db(
        [("m1", OLD, "Quiet Hours", FINGERPRINT), ("m2", OLD, "Quiet Hours", FINGERPRINT)],
        _messages(),
        fail_update=True,
    )
    annotator = _wire(monkeypatch, db)

    assert await reann.main(["--user", "claude", "--rewrite"]) == 1

    assert annotator.await_count == 2  # the second memory was still attempted
    assert db.conn.rollback.await_count == 2
    # A failed write never marks the memory done.
    inserts = [w for w in db.writes if "reannotated_ids" in w[0]]
    assert json.loads(inserts[0][1][0]) == []
    db.conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_improvement_never_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _Db([("m1", NEW, "Quiet Hours", FINGERPRINT)], _messages())
    _wire(monkeypatch, db, annotation="Short and generic, intertwined.")

    assert await reann.main(["--user", "claude", "--rewrite"]) == 0

    assert not [w for w in db.writes if w[0].lstrip().startswith("UPDATE")]


@pytest.mark.asyncio
async def test_save_reannotated_ids_failure_is_contained() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=RuntimeError("db gone"))
    conn.rollback = AsyncMock()
    conn.commit = AsyncMock()

    assert await reann._save_reannotated_ids(conn, {"m1"}) is False

    conn.rollback.assert_awaited_once()
    conn.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_database_url_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reann, "DATABASE_URL", "")
    assert await reann.main([]) == 1
