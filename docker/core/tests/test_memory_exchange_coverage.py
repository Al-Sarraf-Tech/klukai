"""Behavioral coverage for app.memory_exchange — Postgres exchange store + recall.

These functions are bound onto MemoryManager, but each takes an explicit
`self`, so we test the unbound exports directly with a hand-built fake `self`.
Postgres is a recording fake connection; Claude Haiku is a stub. No network,
no vector store.

Behaviors asserted:
- store_exchange: one insert-only companion_exchanges row per exchange, full
  text, scoped to user_id; a database failure is swallowed (SACRED chat path).
- recall_exchanges: Postgres candidates scoped to user, Haiku's pick and order,
  stored importance projected; Postgres order when Haiku is unavailable; []
  on any error.
- recall_exchanges_with_recency: recency + affection re-ranking and ordering.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.memory  # noqa: F401,E402  (settles the manager/mixin imports)
from app.memory_exchange import (  # noqa: E402
    _days_ago,
    recall_exchanges,
    recall_exchanges_with_recency,
    store_exchange,
)


class _RecordingConn:
    """Async connection recording every SQL string + params it executes."""

    def __init__(self, rows=None):
        self.calls: list[tuple[str, tuple]] = []
        self._rows = rows or []

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        result = MagicMock()
        result.fetchall = AsyncMock(return_value=list(self._rows))
        return result

    async def commit(self):
        pass

    async def rollback(self):
        pass


def _conn_ctx(conn):
    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


def _broken():
    raise RuntimeError("pg down")


# ── store_exchange ───────────────────────────────────────────────────────────


class TestStoreExchange:
    @pytest.mark.asyncio
    async def test_inserts_one_user_scoped_row_without_a_vector(self):
        obj = MagicMock()
        conn = _RecordingConn()
        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            await store_exchange(
                obj, "ex-1", "hello there", "hi commander",
                topics=["greeting"], mood="warm", importance=0.8,
                conversation_id="conv-1", user_id="alice",
            )
        assert len(conn.calls) == 1
        sql, params = conn.calls[0]
        assert "INSERT INTO companion_exchanges" in sql
        assert params == (
            "ex-1", "conv-1", "hello there", "hi commander", ["greeting"],
            "warm", 0.8, "alice",
        )
        # Nothing else is contacted: no embedding call, no vector store.
        assert obj.mock_calls == []

    @pytest.mark.asyncio
    async def test_store_is_insert_only(self):
        conn = _RecordingConn()
        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            await store_exchange(MagicMock(), "ex-9", "u", "a", topics=[])
        sql, params = conn.calls[0]
        assert "ON CONFLICT (id) DO NOTHING" in sql
        assert "DELETE" not in sql.upper() and "UPDATE" not in sql.upper()
        # Defaults: composed mood, mid importance, no conversation, owner user.
        assert params[1] is None and params[5:] == ("composed", 0.5, "jalsarraf")

    @pytest.mark.asyncio
    async def test_keeps_full_untruncated_text(self):
        conn = _RecordingConn()
        long_user, long_asst = "U" * 5000, "Z" * 5000
        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            await store_exchange(MagicMock(), "ex-2", long_user, long_asst, topics=[])
        params = conn.calls[0][1]
        assert params[2] == long_user and params[3] == long_asst

    @pytest.mark.asyncio
    async def test_database_failure_is_swallowed_and_logged(self, caplog):
        import logging

        with patch("app.db.get_conn_autocommit", side_effect=_broken), \
             caplog.at_level(logging.ERROR, logger="app.memory_exchange"):
            # Must not raise — the chat path stays alive.
            await store_exchange(MagicMock(), "ex-12", "u", "a", topics=[])
        assert any("Failed to store exchange" in r.getMessage() for r in caplog.records)


# ── recall_exchanges ─────────────────────────────────────────────────────────


_WHEN = datetime(2026, 10, 20, 9, 30, tzinfo=timezone.utc)


def _row(n, uc, ac, topics=None, mood="composed", importance=0.5, created_at=_WHEN):
    return (f"ex-{n}", uc, ac, topics, mood, importance, created_at)


class TestRecallExchanges:
    @pytest.mark.asyncio
    async def test_maps_haiku_pick_and_scopes_to_user(self):
        rows = [
            _row(1, "q1", "a1", topics=["t"], mood="warm", importance=0.87),
            _row(2, "q2", "a2"),
            _row(3, "q3", "a3", topics=None, mood=None, importance=None, created_at=None),
        ]
        conn = _RecordingConn(rows=rows)
        pick = AsyncMock(return_value={"ids": ["3", "1"]})
        with patch("app.db.get_conn", _conn_ctx(conn)), \
             patch("app.background_llm.json_object", pick):
            out = await recall_exchanges(MagicMock(), "find me", limit=4, user_id="bob")

        sql, params = conn.calls[0]
        assert "FROM companion_exchanges" in sql and "ts_rank" in sql
        assert params == ("find or me", "bob", 40)
        assert pick.await_args.args[0] == "memory_recall_exchanges"
        assert [e["user_content"] for e in out] == ["q3", "q1"]
        assert out[0] == {
            "user_content": "q3", "assistant_content": "a3", "topics": [],
            "mood": "composed", "score": 1.0, "created_at": "", "importance": 0.5,
        }
        assert out[1]["topics"] == ["t"] and out[1]["mood"] == "warm"
        assert out[1]["importance"] == pytest.approx(0.87)
        assert out[1]["score"] == 0.5
        assert out[1]["created_at"] == _WHEN.isoformat()

    @pytest.mark.asyncio
    async def test_candidate_text_shows_both_sides_and_topics(self):
        conn = _RecordingConn(rows=[_row(1, "I love rain", "Then stay.", topics=["rain"])])
        pick = AsyncMock(return_value=None)
        with patch("app.db.get_conn", _conn_ctx(conn)), \
             patch("app.background_llm.json_object", pick):
            await recall_exchanges(MagicMock(), "rain")
        prompt = pick.await_args.args[1]
        assert "[2026-10-20] Commander: I love rain | Klukai: Then stay. (topics: rain)" in prompt

    @pytest.mark.asyncio
    async def test_haiku_unavailable_keeps_postgres_order_capped(self):
        rows = [_row(n, f"q{n}", "a") for n in range(1, 9)]
        conn = _RecordingConn(rows=rows)
        with patch("app.db.get_conn", _conn_ctx(conn)), \
             patch("app.background_llm.json_object", AsyncMock(return_value=None)):
            out = await recall_exchanges(MagicMock(), "q", limit=3)
        assert [e["user_content"] for e in out] == ["q1", "q2", "q3"]

    @pytest.mark.asyncio
    async def test_empty_candidates_return_empty_without_haiku(self):
        conn = _RecordingConn(rows=[])
        pick = AsyncMock()
        with patch("app.db.get_conn", _conn_ctx(conn)), \
             patch("app.background_llm.json_object", pick):
            assert await recall_exchanges(MagicMock(), "q") == []
        pick.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_haiku_saying_nothing_is_relevant_returns_empty(self):
        conn = _RecordingConn(rows=[_row(1, "q1", "a1")])
        with patch("app.db.get_conn", _conn_ctx(conn)), \
             patch("app.background_llm.json_object", AsyncMock(return_value={"ids": []})):
            assert await recall_exchanges(MagicMock(), "q") == []

    @pytest.mark.asyncio
    async def test_database_error_returns_empty(self):
        with patch("app.db.get_conn", side_effect=_broken):
            assert await recall_exchanges(MagicMock(), "q") == []


# ── recall_exchanges_with_recency ────────────────────────────────────────────
#
# recall_exchanges_with_recency calls `self.recall_exchanges(...)` (the bound
# method), so we stub it on the instance via an AsyncMock.


def _self_with_recall(rows):
    obj = MagicMock()
    obj.recall_exchanges = AsyncMock(return_value=rows)
    return obj


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TestRecallWithRecency:
    @pytest.mark.asyncio
    async def test_empty_recall_returns_empty(self):
        obj = _self_with_recall([])
        out = await recall_exchanges_with_recency(obj, "q")
        assert out == []

    @pytest.mark.asyncio
    async def test_doubles_limit_for_reranking_window(self):
        """It over-fetches (limit*2) so recency re-ranking has headroom."""
        obj = _self_with_recall([])
        await recall_exchanges_with_recency(obj, "q", limit=5, user_id="bob")
        assert obj.recall_exchanges.call_args.kwargs["limit"] == 10
        assert obj.recall_exchanges.call_args.kwargs["user_id"] == "bob"

    @pytest.mark.asyncio
    async def test_recent_outranks_old_with_close_scores(self):
        now = datetime.now(timezone.utc)
        recent = {
            "user_content": "recent", "assistant_content": "r",
            "topics": [], "mood": "composed", "score": 0.80,
            "created_at": now.isoformat(),
        }
        old = {
            "user_content": "old", "assistant_content": "o",
            "topics": [], "mood": "composed", "score": 0.82,
            "created_at": (now - timedelta(days=365)).isoformat(),
        }
        obj = _self_with_recall([old, recent])  # old listed first; recency flips it
        out = await recall_exchanges_with_recency(obj, "q", limit=2)
        assert [e["user_content"] for e in out] == ["recent", "old"]
        assert out[0]["final_score"] > out[1]["final_score"]

    @pytest.mark.asyncio
    async def test_naive_legacy_timestamps_are_read_as_local_time(self):
        """Old naive stamps must not crash the aware-UTC arithmetic."""
        obj = _self_with_recall([{
            "user_content": "x", "assistant_content": "y", "score": 0.5,
            "created_at": datetime.now().isoformat(),
        }])
        out = await recall_exchanges_with_recency(obj, "q", limit=1, affection_level=4)
        expected = (1 - 0.15) * 0.5 + 0.15 * 1.0
        assert out[0]["final_score"] == pytest.approx(expected, abs=1e-3)

    @pytest.mark.asyncio
    async def test_truncates_to_limit_after_rerank(self):
        now = _now_iso()
        rows = [
            {"user_content": f"u{i}", "assistant_content": "a", "topics": [],
             "mood": "composed", "score": 0.5 + i * 0.01, "created_at": now}
            for i in range(6)
        ]
        obj = _self_with_recall(rows)
        out = await recall_exchanges_with_recency(obj, "q", limit=3)
        assert len(out) == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize("created_at", ["not-a-date", "", None])
    async def test_bad_created_at_defaults_to_30_days(self, created_at):
        row = {"user_content": "x", "assistant_content": "y", "score": 0.5}
        if created_at is not None:
            row["created_at"] = created_at
        obj = _self_with_recall([row])
        # affection_level=4 sits between the bias bands: no importance bias.
        out = await recall_exchanges_with_recency(obj, "q", limit=1, affection_level=4)
        expected = (1 - 0.15) * 0.5 + 0.15 * (1.0 / 31.0)
        assert out[0]["final_score"] == pytest.approx(expected, abs=1e-6)

    @pytest.mark.asyncio
    async def test_high_affection_biases_toward_importance(self):
        row = {
            "user_content": "imp", "assistant_content": "a",
            "topics": [], "mood": "composed", "score": 0.5,
            "importance": 1.0, "created_at": _now_iso(),
        }
        high = await recall_exchanges_with_recency(
            _self_with_recall([dict(row)]), "q", limit=1, affection_level=9)
        neutral = await recall_exchanges_with_recency(
            _self_with_recall([dict(row)]), "q", limit=1, affection_level=4)
        assert high[0]["final_score"] == pytest.approx(neutral[0]["final_score"] + 0.2, abs=1e-3)

    @pytest.mark.asyncio
    async def test_low_affection_biases_toward_unimportant(self):
        obj = _self_with_recall([{
            "user_content": "x", "assistant_content": "y",
            "topics": [], "mood": "composed", "score": 0.5,
            "importance": 0.0, "created_at": _now_iso(),
        }])
        out = await recall_exchanges_with_recency(obj, "q", limit=1, affection_level=1)
        base = (1 - 0.15) * 0.5 + 0.15 * 1.0
        assert out[0]["final_score"] == pytest.approx(base + 0.1, abs=1e-3)

    @pytest.mark.asyncio
    async def test_stored_importance_flows_through_real_recall(self):
        """End-to-end: the re-rank reads the IMPORTANCE the real recall_exchanges
        projects from the stored row (not a constant 0.5), on top of the
        rank-derived score. Haiku is unavailable, so the Postgres order stands."""
        import types

        now = datetime.now(timezone.utc)
        rows = [
            _row(1, "low", "a", importance=0.1, created_at=now),
            _row(2, "high", "a", importance=0.9, created_at=now),
        ]
        conn = _RecordingConn(rows=rows)
        obj = MagicMock()
        obj.recall_exchanges = types.MethodType(recall_exchanges, obj)
        with patch("app.db.get_conn", _conn_ctx(conn)), \
             patch("app.background_llm.json_object", AsyncMock(return_value=None)):
            out = await recall_exchanges_with_recency(obj, "q", limit=2, affection_level=9)

        by_uc = {e["user_content"]: e for e in out}
        # Rank scores 1.0 vs 0.5 (weighted 0.85) against an importance bias of
        # 0.9*0.2 vs 0.1*0.2.
        assert by_uc["low"]["final_score"] == pytest.approx(
            0.85 * 1.0 + 0.15 + 0.1 * 0.2, abs=1e-3)
        assert by_uc["high"]["final_score"] == pytest.approx(
            0.85 * 0.5 + 0.15 + 0.9 * 0.2, abs=1e-3)


def test_days_ago_reads_aware_and_naive_stamps():
    now = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    assert _days_ago((now - timedelta(days=2)).isoformat(), now) == pytest.approx(2.0)
    assert _days_ago((now + timedelta(days=1)).isoformat(), now) == 0.0  # clock skew
    assert _days_ago("garbage", now) == 30.0
