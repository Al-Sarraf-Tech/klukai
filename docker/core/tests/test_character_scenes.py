"""Atomic once-ever oath persistence, with no live database."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.character_scenes import commit_oath_scene, oath_lines
from app import background


class _Cursor:
    def __init__(self, *, rowcount: int = 1, row=None):
        self.rowcount = rowcount
        self._row = row

    async def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, *, claim: bool = True, conversation=None, fail_sql: str | None = None):
        self.claim = claim
        self.conversation = conversation
        self.fail_sql = fail_sql
        self.statements: list[tuple[str, object]] = []
        self.committed = False

    async def execute(self, statement, params=None):
        self.statements.append((statement, params))
        if self.fail_sql and self.fail_sql in statement:
            raise RuntimeError("injected failure")
        if "INSERT INTO companion_firsts" in statement:
            return _Cursor(rowcount=1 if self.claim else 0)
        if "SELECT id FROM companion_conversations" in statement:
            return _Cursor(row=self.conversation)
        if "INSERT INTO companion_conversations" in statement:
            return _Cursor(row=("created-conv",))
        return _Cursor()

    async def commit(self):
        self.committed = True


def _pool(conn: _Conn):
    @asynccontextmanager
    async def get_conn():
        yield conn

    return patch("app.character_scenes.get_conn", get_conn)


def test_invalid_scene_shapes_are_rejected():
    assert oath_lines(None) is None
    assert oath_lines([]) is None
    assert oath_lines(["Stay.", ""]) is None
    assert oath_lines(["Stay.", 3]) is None
    assert oath_lines(["Stay.", "  "]) is None
    assert oath_lines(["Stay.", "I chose you."]) == ["Stay.", "I chose you."]


async def test_invalid_scene_does_not_open_a_transaction():
    with patch("app.character_scenes.get_conn", side_effect=AssertionError("db")):
        assert await commit_oath_scene("u1", ["Stay.", ""]) is False


async def test_existing_trigger_writes_nothing():
    conn = _Conn(claim=False)
    with _pool(conn):
        assert await commit_oath_scene("u1", ["Stay.", "I chose you."]) is False
    assert conn.committed is False
    assert [sql for sql, _ in conn.statements] == [
        "INSERT INTO companion_firsts (user_id, event_type, event_date, metadata) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (user_id, event_type) DO NOTHING"
    ]


async def test_commit_appends_every_line_before_returning():
    conn = _Conn(conversation=("existing",))
    with _pool(conn):
        assert await commit_oath_scene("u1", ["Stay.", "I chose you."]) is True
    assert conn.committed is True
    written = [params[1] for sql, params in conn.statements if "INSERT INTO companion_messages" in sql]
    assert written == ["Stay.", "I chose you."]
    update = [params for sql, params in conn.statements if sql.startswith("UPDATE")]
    assert update == [(2, "existing")]


async def test_missing_conversation_is_created_in_the_same_transaction():
    conn = _Conn(conversation=None)
    with _pool(conn):
        assert await commit_oath_scene("u1", ["Stay."]) is True
    assert any("INSERT INTO companion_conversations" in sql for sql, _ in conn.statements)
    assert conn.committed is True


async def test_message_failure_does_not_commit():
    conn = _Conn(conversation=("existing",), fail_sql="INSERT INTO companion_messages")
    with _pool(conn):
        assert await commit_oath_scene("u1", ["Stay.", "I chose you."]) is False
    assert conn.committed is False


async def test_live_pacing_starts_only_after_the_full_scene_is_committed():
    order: list[str] = []

    async def commit(user_id, lines):
        order.append(f"commit:{user_id}:{len(lines)}")
        return True

    async def send(_user_id, message, *, persist=True):
        order.append(f"send:{message}:{persist}")
        if message == "I chose you.":
            raise asyncio.CancelledError

    with patch("app.background.load_personality", return_value={
        "affection": {"oath_fulfilled_scene": ["Stay.", "I chose you."]}
    }), patch("app.background.commit_oath_scene", commit), \
         patch("app.background.ws.send_proactive", send), \
         patch("app.background.asyncio.sleep", AsyncMock()):
        with pytest.raises(asyncio.CancelledError):
            await background.maybe_deliver_oath("u1")
    assert order == [
        "commit:u1:2",
        "send:Stay.:False",
        "send:I chose you.:False",
    ]


async def test_failed_commit_does_not_send():
    ws = AsyncMock()
    with patch("app.background.load_personality", return_value={
        "affection": {"oath_fulfilled_scene": ["Stay."]}
    }), patch("app.background.commit_oath_scene", AsyncMock(return_value=False)), \
         patch("app.background.ws.send_proactive", ws):
        assert await background.maybe_deliver_oath("u1") is False
    ws.assert_not_awaited()
