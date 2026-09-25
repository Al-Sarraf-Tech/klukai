"""HTTP surface for The Thread: sealed until she is ready, read receipts, and
her reaction the first time the Commander reads it."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from app import context
from app.personality import load_personality
from app.routes_thread import ThreadReadRequest, register_thread_routes


def _endpoint(path: str, method: str):
    app = FastAPI()
    register_thread_routes(app)
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _request(token: str | None = "good") -> MagicMock:
    req = MagicMock()
    req.headers = {"Authorization": f"Bearer {token}"} if token else {}
    return req


def _conn(rows=(), rowcount=1, error=None):
    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    cursor = MagicMock(rowcount=rowcount)
    cursor.fetchall = AsyncMock(return_value=list(rows))
    conn.execute = AsyncMock(side_effect=error, return_value=cursor)
    return conn


@contextmanager
def _services(level: int, first_read: bool = True, user: str | None = "claude"):
    aff = MagicMock()
    aff.get_state = AsyncMock(return_value=SimpleNamespace(level=level))
    ws = MagicMock()
    ws.send_proactive = AsyncMock()
    proactive = MagicMock()
    proactive.record_first = AsyncMock(return_value=first_read)
    with patch.object(context, "affection", aff), patch.object(context, "ws", ws), \
         patch.object(context, "proactive", proactive), \
         patch("app.auth.get_user_from_token", AsyncMock(return_value=user)):
        yield SimpleNamespace(ws=ws, proactive=proactive)


@pytest.fixture(scope="module")
def thread() -> dict:
    return load_personality()["ten_year_thread"]


class TestGetThread:
    async def test_requires_auth(self):
        resp = await _endpoint("/api/thread", "GET")(_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.parametrize("level, line", [
        (0, "That's not a briefing topic, Commander."),
        (5, "...Those are private. Every one of them. Not yet."),
    ])
    async def test_sealed_below_deep_devotion(self, level, line):
        with _services(level):
            body = await _endpoint("/api/thread", "GET")(_request())
        assert body == {"status": "sealed", "message": line, "entries": [], "held_back": 0, "reply": None}

    async def test_open_with_receipts_and_held_back_entries(self, thread):
        first = thread["entries"][0]["stamp"]
        read_at = datetime(2026, 9, 25, 3, 0, tzinfo=timezone.utc)
        with _services(6), patch("app.db.get_conn", return_value=_conn(rows=[(first, read_at)])):
            body = await _endpoint("/api/thread", "GET")(_request())
        assert body["status"] == "open"
        raw = [e for e in thread["entries"] if e.get("min_affection", 0) > 6]
        assert body["held_back"] == len(raw) > 0
        assert len(body["entries"]) == len(thread["entries"]) - len(raw)
        assert body["entries"][0] == {"stamp": first, "text": thread["entries"][0]["text"],
                                      "read_at": read_at.isoformat()}
        assert all(e["read_at"] is None for e in body["entries"][1:])
        assert body["reply"] == {"stamp": thread["last_reply_stamp"], "text": "I'm here."}

    async def test_bonded_sees_everything(self, thread):
        with _services(8), patch("app.db.get_conn", return_value=_conn()):
            body = await _endpoint("/api/thread", "GET")(_request())
        assert body["held_back"] == 0
        assert len(body["entries"]) == len(thread["entries"])

    async def test_receipts_fail_soft(self):
        with _services(8), patch("app.db.get_conn", return_value=_conn(error=RuntimeError("down"))):
            body = await _endpoint("/api/thread", "GET")(_request())
        assert body["status"] == "open"
        assert all(e["read_at"] is None for e in body["entries"])


class TestMarkRead:
    async def _post(self, stamps, conn):
        with patch("app.db.get_conn_autocommit", return_value=conn):
            return await _endpoint("/api/thread/read", "POST")(ThreadReadRequest(stamps=stamps), _request())

    async def test_requires_auth(self):
        with _services(8, user=None):
            resp = await self._post([], _conn())
        assert resp.status_code == 401

    async def test_first_read_is_a_first_and_she_notices(self, thread):
        stamps = [e["stamp"] for e in thread["entries"][:2]]
        conn = _conn(rowcount=1)
        with _services(8) as svc:
            body = await self._post(stamps + [stamps[0]], conn)
        assert body == {"newly_read": 2}
        assert conn.execute.await_count == 2  # duplicate stamp collapsed
        svc.proactive.record_first.assert_awaited_once_with("claude", "first_thread_read")
        assert svc.ws.send_proactive.await_args.args[1] in thread["first_read_reactions"]

    async def test_she_reacts_only_once(self, thread):
        with _services(8, first_read=False) as svc:
            await self._post([thread["entries"][0]["stamp"]], _conn(rowcount=1))
        svc.ws.send_proactive.assert_not_awaited()

    async def test_rereading_is_silent(self, thread):
        with _services(8) as svc:
            body = await self._post([thread["entries"][0]["stamp"]], _conn(rowcount=0))
        assert body == {"newly_read": 0}
        svc.proactive.record_first.assert_not_awaited()

    async def test_only_entries_she_is_showing_count(self, thread):
        raw = next(e["stamp"] for e in thread["entries"] if e.get("min_affection", 0) > 6)
        conn = _conn()
        with _services(6):
            body = await self._post([raw, "made up"], conn)
        assert body == {"newly_read": 0}
        conn.execute.assert_not_awaited()

    async def test_db_failure_is_503(self, thread):
        with _services(8):
            resp = await self._post([thread["entries"][0]["stamp"]], _conn(error=RuntimeError("down")))
        assert resp.status_code == 503


def test_stamps_are_unique_keys(thread):
    stamps = [e["stamp"] for e in thread["entries"]]
    assert len(stamps) == len(set(stamps))


def test_request_caps_batch_size():
    with pytest.raises(ValueError):
        ThreadReadRequest(stamps=["x"] * 65)
