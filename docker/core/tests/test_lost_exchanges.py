"""A chat turn that never reached her exchange memory comes back from the message log.

Extraction runs in a memory-only queue: a restart, a full backlog, repeated
preemption or a database blip all used to lose the exchange for good, although
companion_messages still held the turn. The real-PostgreSQL half needs
KLUKAI_MIGRATION_TEST_DATABASE_URL (a disposable database), like
test_memory_recall_postgres.py.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import logging
import os
from pathlib import Path
import uuid

import psycopg
from psycopg import sql
from psycopg_pool import AsyncConnectionPool
import pytest

from app import db
from app.helpers import TRIVIAL_PATTERNS, is_trivial_message
from app.memory_exchange import restore_lost_exchanges
from app.migrations import apply_migrations

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
NOW = datetime.now(UTC)
OLD = NOW - timedelta(days=2)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("OK!!", True),
        ("  thanks. ", True),
        ("hey", True),
        ("Tell me about the storm last night", False),
    ],
)
def test_trivial_message(content, expected):
    assert is_trivial_message(content) is expected


def test_a_short_message_that_asked_for_a_picture_is_not_trivial():
    assert is_trivial_message("pic", image_triggered=True) is False
    assert is_trivial_message("ok", image_triggered=True) is True


def test_callers_can_supply_their_own_patterns():
    assert is_trivial_message("a longer phrase", {"a longer phrase"}) is True
    assert "ok" in TRIVIAL_PATTERNS


class _Cursor:
    def __init__(self, rows=(), rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    async def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows, inserted=1):
        self.rows = rows
        self.inserted = inserted
        self.inserts = []

    async def execute(self, query, params=None):
        if params is None:
            return _Cursor(self.rows)
        self.inserts.append(params)
        return _Cursor(rowcount=self.inserted)


def _use(monkeypatch, conn):
    @asynccontextmanager
    async def fake():
        yield conn

    monkeypatch.setattr("app.db.get_conn_autocommit", fake)


def _row(user_content="Tell me about the storm last night", mood="tender"):
    return ("reply-id", "conv-id", "alice", user_content, "It was loud.", mood, OLD)


async def test_restores_a_lost_turn_and_counts_it(monkeypatch, caplog):
    conn = _Conn([_row()])
    _use(monkeypatch, conn)
    with caplog.at_level(logging.INFO, logger="app.memory_exchange"):
        assert await restore_lost_exchanges() == 1
    assert conn.inserts == [
        (
            "reply-id",
            "conv-id",
            "Tell me about the storm last night",
            "It was loud.",
            "tender",
            0.4,
            "alice",
            OLD,
        )
    ]
    # A count only: never what was said.
    assert "Restored 1 lost exchange" in caplog.text
    assert "storm" not in caplog.text


async def test_a_turn_with_no_mood_reads_composed(monkeypatch):
    conn = _Conn([_row(mood=None)])
    _use(monkeypatch, conn)
    await restore_lost_exchanges()
    assert conn.inserts[0][4] == "composed"


async def test_skips_the_turns_extraction_skips_on_purpose(monkeypatch, caplog):
    conn = _Conn([_row(user_content="thanks!")])
    _use(monkeypatch, conn)
    with caplog.at_level(logging.INFO, logger="app.memory_exchange"):
        assert await restore_lost_exchanges() == 0
    assert conn.inserts == []
    assert "Restored" not in caplog.text


async def test_a_row_that_already_exists_is_not_counted(monkeypatch, caplog):
    conn = _Conn([_row()], inserted=0)
    _use(monkeypatch, conn)
    with caplog.at_level(logging.INFO, logger="app.memory_exchange"):
        assert await restore_lost_exchanges() == 0
    assert "Restored" not in caplog.text


@pytest.fixture
async def real_db(monkeypatch):
    url = os.environ.get("KLUKAI_MIGRATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set KLUKAI_MIGRATION_TEST_DATABASE_URL to a disposable PostgreSQL database")
    schema = "lost_exchanges_test_" + uuid.uuid4().hex
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            async with AsyncConnectionPool(
                url,
                min_size=1,
                max_size=4,
                open=False,
                kwargs={"options": f"-c search_path={schema}"},
            ) as pool:
                await pool.wait()
                await apply_migrations(pool, MIGRATIONS)
                monkeypatch.setattr(db, "_pool", pool)
                yield pool
        finally:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


async def _conversation(user_id="alice"):
    conversation = str(uuid.uuid4())
    async with db.get_conn_autocommit() as conn:
        await conn.execute(
            "INSERT INTO companion_conversations (id, user_id) VALUES (%s, %s)",
            (conversation, user_id),
        )
    return conversation


async def _message(conversation, role, content, at, *, user_id="alice", kind="text", mood=None):
    message = str(uuid.uuid4())
    async with db.get_conn_autocommit() as conn:
        await conn.execute(
            "INSERT INTO companion_messages "
            "(id, conversation_id, role, content, content_type, mood, user_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (message, conversation, role, content, kind, mood, user_id, at),
        )
    return message


async def _turn(conversation, said, reply, at, *, user_id="alice", mood=None):
    await _message(conversation, "user", said, at, user_id=user_id)
    return await _message(
        conversation, "assistant", reply, at + timedelta(seconds=9), user_id=user_id, mood=mood
    )


async def _store(said, reply, at, *, user_id="alice", importance=0.7):
    async with db.get_conn_autocommit() as conn:
        await conn.execute(
            "INSERT INTO companion_exchanges "
            "(id, user_content, assistant_content, topics, importance, user_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (str(uuid.uuid4()), said, reply, ["kept"], importance, user_id, at),
        )


async def _exchanges():
    async with db.get_conn_autocommit() as conn:
        rows = await (
            await conn.execute(
                "SELECT user_id, user_content, assistant_content, mood, importance, "
                "topics, created_at, id::text, conversation_id::text "
                "FROM companion_exchanges ORDER BY created_at"
            )
        ).fetchall()
    return rows


async def test_real_db_restores_a_lost_turn_once(real_db):
    conversation = await _conversation()
    reply = await _turn(conversation, "the storm last night", "It was loud.", OLD, mood="tender")

    assert await restore_lost_exchanges() == 1
    assert await restore_lost_exchanges() == 0

    rows = await _exchanges()
    assert len(rows) == 1
    user_id, said, answer, mood, importance, topics, at, row_id, row_conversation = rows[0]
    assert (user_id, said, answer, mood, topics) == (
        "alice",
        "the storm last night",
        "It was loud.",
        "tender",
        [],
    )
    assert importance == pytest.approx(0.4)
    # Recall ranks by age, so it keeps the turn's own time, not the repair's.
    assert at == OLD + timedelta(seconds=9)
    assert (row_id, row_conversation) == (reply, conversation)


async def test_real_db_restored_turns_are_searchable(real_db):
    conversation = await _conversation()
    await _turn(conversation, "the thunderstorm last night", "It was loud.", OLD)
    await restore_lost_exchanges()
    async with db.get_conn_autocommit() as conn:
        row = await (
            await conn.execute(
                "SELECT count(*) FROM companion_exchanges "
                "WHERE search_tsv @@ websearch_to_tsquery('english', 'thunderstorm')"
            )
        ).fetchone()
    assert row[0] == 1


async def test_real_db_a_stored_turn_stays_as_it_was(real_db):
    conversation = await _conversation()
    await _turn(conversation, "the storm last night", "It was loud.", OLD)
    await _store("the storm last night", "It was loud.", OLD + timedelta(seconds=30))
    before = await _exchanges()

    assert await restore_lost_exchanges() == 0
    assert await _exchanges() == before


async def test_real_db_stored_text_that_differs_on_her_side_still_counts(real_db):
    conversation = await _conversation()
    await _turn(conversation, "the storm last night", "It was loud. *smiles*", OLD)
    await _store("the storm last night", "It was loud.", OLD + timedelta(seconds=30))

    assert await restore_lost_exchanges() == 0


async def test_real_db_the_same_words_on_another_day_do_not_count(real_db):
    conversation = await _conversation()
    await _turn(conversation, "the storm last night", "It was loud.", OLD)
    await _store("the storm last night", "Earlier reply.", OLD - timedelta(days=3))

    assert await restore_lost_exchanges() == 1


async def test_real_db_another_accounts_exchange_does_not_count(real_db):
    conversation = await _conversation()
    await _turn(conversation, "the storm last night", "It was loud.", OLD)
    await _store(
        "the storm last night", "It was loud.", OLD + timedelta(seconds=30), user_id="bob"
    )

    assert await restore_lost_exchanges() == 1
    assert [row[0] for row in await _exchanges()].count("alice") == 1


async def test_real_db_leaves_a_recent_turn_for_its_queued_extraction(real_db):
    conversation = await _conversation()
    await _turn(conversation, "the storm last night", "It was loud.", NOW - timedelta(minutes=30))

    assert await restore_lost_exchanges() == 0


async def test_real_db_leaves_turns_past_the_lookback(real_db):
    conversation = await _conversation()
    await _turn(conversation, "the storm last night", "It was loud.", NOW - timedelta(days=45))

    assert await restore_lost_exchanges() == 0


async def test_real_db_leaves_trivial_turns(real_db):
    conversation = await _conversation()
    await _turn(conversation, "thanks!", "Anytime.", OLD)

    assert await restore_lost_exchanges() == 0


async def test_real_db_her_own_line_between_them_does_not_break_the_pair(real_db):
    conversation = await _conversation()
    await _message(conversation, "user", "the storm last night", OLD)
    await _message(
        conversation, "assistant", "A random line.", OLD + timedelta(seconds=2), kind="proactive"
    )
    await _message(conversation, "assistant", "It was loud.", OLD + timedelta(seconds=9))

    assert await restore_lost_exchanges() == 1
    assert (await _exchanges())[0][2] == "It was loud."


async def test_real_db_her_own_line_alone_is_not_a_reply(real_db):
    conversation = await _conversation()
    await _message(conversation, "user", "the storm last night", OLD)
    await _message(
        conversation, "assistant", "A random line.", OLD + timedelta(seconds=2), kind="proactive"
    )

    assert await restore_lost_exchanges() == 0


async def test_real_db_a_message_she_never_answered_is_not_a_turn(real_db):
    conversation = await _conversation()
    await _message(conversation, "user", "the first thing I said", OLD)
    await _turn(conversation, "the storm last night", "It was loud.", OLD + timedelta(minutes=1))

    assert await restore_lost_exchanges() == 1
    assert (await _exchanges())[0][1] == "the storm last night"
