"""Tests for promises & gentle accountability.

Covers app/promises.py (store / due / mark / resolve / followup_message) with
the DB fully mocked via get_conn, plus the followup_message tone-by-affection
behavior. extract_promises is covered in test_fact_extractor.py; the route
endpoints in test_routes_extras3_coverage.py.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Fake DB plumbing (mirrors get_conn() as an async context manager) ───────


class _FakeConn:
    """Records executed SQL + params; serves a queue of fetch results."""

    def __init__(self, fetchone=None, fetchall=None):
        self.executed: list[tuple] = []
        self._fetchone = fetchone
        self._fetchall = fetchall or []

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        cur = MagicMock()
        cur.fetchone = AsyncMock(return_value=self._fetchone)
        cur.fetchall = AsyncMock(return_value=self._fetchall)
        return cur

    async def commit(self):
        return None


def _patch_get_conn(conn: _FakeConn):
    @asynccontextmanager
    async def _cm():
        yield conn

    return patch("app.promises.get_conn", _cm)


# ═══════════════════════════════════════════════════════════════════════════
# store_promise
# ═══════════════════════════════════════════════════════════════════════════


class TestStorePromise:
    @pytest.mark.asyncio
    async def test_inserts_and_returns_uuid(self):
        from app import promises

        new_id = "11111111-1111-1111-1111-111111111111"
        conn = _FakeConn(fetchone=(new_id,))
        with _patch_get_conn(conn):
            result = await promises.store_promise(
                {"action": "fix the door", "deadline_hint": "tomorrow",
                 "target": "the door", "confidence": 0.9}
            )

        assert result == new_id
        # One INSERT executed, parameterized (no f-string injection of values).
        # The id is DB-defaulted (gen_random_uuid) + RETURNING'd, so params are
        # (user_id, promise_text, commitment_json, scheduled_followup).
        sql, params = conn.executed[0]
        assert "INSERT INTO companion_promises" in sql
        assert "RETURNING id" in sql
        assert "%s" in sql
        # promise_text derived from the action; commitment serialized as JSON text.
        assert params[0] == "jalsarraf"  # default user_id
        assert params[1] == "fix the door"  # promise_text
        assert isinstance(params[2], str) and "fix the door" in params[2]

    @pytest.mark.asyncio
    async def test_default_followup_when_no_deadline_hint(self):
        from app import promises

        conn = _FakeConn(fetchone=("abc",))
        before = datetime.now(timezone.utc)
        with _patch_get_conn(conn):
            await promises.store_promise({"action": "call mom", "confidence": 0.8})

        # scheduled_followup is the 4th positional param (index 3) — a tz-aware
        # datetime roughly +20h out (the sane default when no hint is given).
        scheduled = conn.executed[0][1][3]
        assert isinstance(scheduled, datetime)
        delta = scheduled - before
        assert timedelta(hours=18) < delta < timedelta(hours=22)

    @pytest.mark.asyncio
    async def test_explicit_followup_overrides_default(self):
        from app import promises

        conn = _FakeConn(fetchone=("abc",))
        when = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
        with _patch_get_conn(conn):
            await promises.store_promise(
                {"action": "ship it"}, scheduled_followup=when
            )
        assert conn.executed[0][1][3] == when

    @pytest.mark.asyncio
    async def test_custom_user_id_passed_through(self):
        from app import promises

        conn = _FakeConn(fetchone=("abc",))
        with _patch_get_conn(conn):
            await promises.store_promise({"action": "x"}, user_id="bob")
        assert conn.executed[0][1][0] == "bob"

    @pytest.mark.asyncio
    async def test_empty_action_returns_none_without_db(self):
        from app import promises

        conn = _FakeConn(fetchone=("abc",))
        with _patch_get_conn(conn):
            assert await promises.store_promise({"action": "   "}) is None
            assert await promises.store_promise({}) is None
        assert conn.executed == []  # never touched the DB

    @pytest.mark.asyncio
    async def test_db_failure_returns_none(self):
        from app import promises

        @asynccontextmanager
        async def _boom():
            raise RuntimeError("db down")
            yield  # pragma: no cover

        with patch("app.promises.get_conn", _boom):
            assert await promises.store_promise({"action": "x"}) is None

    @pytest.mark.asyncio
    async def test_no_row_returned_yields_none(self):
        from app import promises

        conn = _FakeConn(fetchone=None)  # RETURNING yielded nothing
        with _patch_get_conn(conn):
            assert await promises.store_promise({"action": "x"}) is None


# ═══════════════════════════════════════════════════════════════════════════
# due_promises  (the scheduler reads this)
# ═══════════════════════════════════════════════════════════════════════════


class TestDuePromises:
    @pytest.mark.asyncio
    async def test_returns_rows_oldest_first(self):
        from app import promises

        now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        made = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
        rows = [
            ("id-1", "fix the door", {"action": "fix the door"}, made,
             made + timedelta(hours=20)),
        ]
        conn = _FakeConn(fetchall=rows)
        with _patch_get_conn(conn):
            out = await promises.due_promises("jalsarraf", now)

        assert len(out) == 1
        p = out[0]
        assert p["id"] == "id-1"
        assert p["promise_text"] == "fix the door"
        assert p["commitment"] == {"action": "fix the door"}
        # Query filters on scheduled_followup <= now AND resolved_at IS NULL,
        # ordered ascending (oldest first).
        sql, params = conn.executed[0]
        assert "resolved_at IS NULL" in sql
        assert "scheduled_followup <= %s" in sql
        assert "ORDER BY scheduled_followup ASC" in sql
        assert params == ("jalsarraf", now)

    @pytest.mark.asyncio
    async def test_empty_when_none_due(self):
        from app import promises

        conn = _FakeConn(fetchall=[])
        with _patch_get_conn(conn):
            out = await promises.due_promises("jalsarraf", datetime.now(timezone.utc))
        assert out == []

    @pytest.mark.asyncio
    async def test_db_failure_returns_empty(self):
        from app import promises

        @asynccontextmanager
        async def _boom():
            raise RuntimeError("db down")
            yield  # pragma: no cover

        with patch("app.promises.get_conn", _boom):
            out = await promises.due_promises("jalsarraf", datetime.now(timezone.utc))
        assert out == []


# ═══════════════════════════════════════════════════════════════════════════
# mark_followup_sent / resolve_promise
# ═══════════════════════════════════════════════════════════════════════════


class TestMarkAndResolve:
    @pytest.mark.asyncio
    async def test_mark_followup_sent_updates_timestamp(self):
        from app import promises

        conn = _FakeConn()
        with _patch_get_conn(conn):
            ok = await promises.mark_followup_sent("id-7")
        assert ok is True
        sql, params = conn.executed[0]
        assert "UPDATE companion_promises" in sql
        assert "followup_sent_at = NOW()" in sql
        assert params == ("id-7",)

    @pytest.mark.asyncio
    async def test_mark_followup_sent_db_failure_returns_false(self):
        from app import promises

        @asynccontextmanager
        async def _boom():
            raise RuntimeError("db down")
            yield  # pragma: no cover

        with patch("app.promises.get_conn", _boom):
            assert await promises.mark_followup_sent("id-7") is False

    @pytest.mark.asyncio
    async def test_resolve_promise_sets_sentiment_and_response(self):
        from app import promises

        conn = _FakeConn()
        with _patch_get_conn(conn):
            ok = await promises.resolve_promise("id-9", "kept", "Done it!")
        assert ok is True
        sql, params = conn.executed[0]
        assert "resolved_at = NOW()" in sql
        assert "sentiment = %s" in sql
        assert "response_text = %s" in sql
        # Only updates rows that aren't already resolved (idempotent).
        assert "resolved_at IS NULL" in sql
        assert params == ("kept", "Done it!", "id-9")

    @pytest.mark.asyncio
    async def test_resolve_promise_allows_null_response(self):
        from app import promises

        conn = _FakeConn()
        with _patch_get_conn(conn):
            ok = await promises.resolve_promise("id-9", "broken")
        assert ok is True
        assert conn.executed[0][1] == ("broken", None, "id-9")

    @pytest.mark.asyncio
    async def test_resolve_promise_db_failure_returns_false(self):
        from app import promises

        @asynccontextmanager
        async def _boom():
            raise RuntimeError("db down")
            yield  # pragma: no cover

        with patch("app.promises.get_conn", _boom):
            assert await promises.resolve_promise("id-9", "kept") is False


# ═══════════════════════════════════════════════════════════════════════════
# followup_message — tone scales with affection (the scheduler renders this)
# ═══════════════════════════════════════════════════════════════════════════


class TestFollowupMessage:
    def _promise(self, action="fix the squeaky door"):
        return {"id": "p1", "promise_text": action,
                "commitment": {"action": action}}

    def test_low_affection_is_light_and_in_character(self):
        from app import promises

        msg = promises.followup_message(self._promise(), affection_level=1)
        assert isinstance(msg, str) and msg
        assert "Commander" in msg
        # The actual commitment is referenced.
        assert "fix the squeaky door" in msg

    def test_high_affection_is_caring(self):
        from app import promises

        low = promises.followup_message(self._promise(), affection_level=1)
        high = promises.followup_message(self._promise(), affection_level=9)
        # Tone differs by affection — not the same canned line.
        assert low != high
        assert "fix the squeaky door" in high

    def test_falls_back_to_promise_text_when_action_missing(self):
        from app import promises

        p = {"id": "p2", "promise_text": "water the plants", "commitment": {}}
        msg = promises.followup_message(p, affection_level=5)
        assert "water the plants" in msg

    def test_handles_missing_everything_gracefully(self):
        from app import promises

        # No action, no promise_text — must still produce a non-empty,
        # in-character nudge rather than crashing or emitting "None".
        msg = promises.followup_message({}, affection_level=5)
        assert isinstance(msg, str) and msg
        assert "None" not in msg

    def test_affection_buckets_distinct(self):
        from app import promises

        p = self._promise()
        mid = promises.followup_message(p, affection_level=5)
        high = promises.followup_message(p, affection_level=8)
        low = promises.followup_message(p, affection_level=0)
        # Three distinct registers (low / mid / high).
        assert len({low, mid, high}) == 3


# ═══════════════════════════════════════════════════════════════════════════
# open_promises (REST list)
# ═══════════════════════════════════════════════════════════════════════════


class TestOpenPromises:
    @pytest.mark.asyncio
    async def test_returns_unresolved_newest_first(self):
        from app import promises

        made = datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc)
        due = datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc)
        rows = [
            ("id-a", "I'll rest", {"action": "rest"}, made, due, None),
            ("id-b", "I'll call", {"action": "call"}, made, due, made),
        ]
        conn = _FakeConn(fetchall=rows)
        with _patch_get_conn(conn):
            out = await promises.open_promises("alice")

        assert [p["id"] for p in out] == ["id-a", "id-b"]
        assert out[0]["followup_sent"] is False
        assert out[1]["followup_sent"] is True
        sql, params = conn.executed[0]
        assert "resolved_at IS NULL" in sql
        assert params[0] == "alice"

    @pytest.mark.asyncio
    async def test_db_failure_returns_empty(self):
        from app import promises

        def _boom():
            raise RuntimeError("db down")

        with patch("app.promises.get_conn", side_effect=_boom):
            out = await promises.open_promises("alice")
        assert out == []
