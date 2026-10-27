"""Behavioral coverage for app/memory.py — episodic (Postgres) store + recall,
combined-recall and nudge tiers.

Complements tests/test_memory_session.py (which covers session/fact/milestone
record) and tests/test_memory_recall.py (the shared candidate + Haiku
selection helpers). Here we exercise the lifecycle, the insert-only episode
store (no vectors, no Qdrant), episode recall through Postgres candidates and
Claude Haiku's pick, the parallel recall_for_prompt fan-out, and the
memory-nudge interval/formatting logic.

Every test asserts concrete behavior: what SQL ran, what got stored, what got
returned, and — for the store path — that we INSERT-ONLY and never DELETE.

All I/O is mocked; datetime/random are frozen for determinism.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _mk_manager():
    """MemoryManager with Redis mocked (no live services)."""
    from app.memory import MemoryManager

    m = MemoryManager()
    m._redis = AsyncMock()
    return m


# ═══════════════════════════════════════════════════════════════════════════
# init / close — lifecycle wiring
# ═══════════════════════════════════════════════════════════════════════════


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_init_builds_clients_without_any_vector_store(self):
        """init() creates the Redis client and touches nothing else: no Qdrant
        collection bootstrap, no embedding service and no HTTP data service
        (facts are read from Postgres by app/fact_store.py)."""
        from app.memory import MemoryManager

        m = MemoryManager()
        fake_redis = MagicMock()

        with patch("app.memory.redis.from_url", return_value=fake_redis) as from_url:
            await m.init()

        assert m._redis is fake_redis
        from_url.assert_called_once()
        for gone in ("embed_text", "is_zero_vector", "_ensure_qdrant_collection",
                     "_ensure_msg_collection", "_http"):
            assert not hasattr(m, gone)

    @pytest.mark.asyncio
    async def test_close_closes_the_redis_client(self):
        m = _mk_manager()
        m._redis.aclose = AsyncMock()

        await m.close()

        m._redis.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_is_safe_when_clients_unset(self):
        from app.memory import MemoryManager

        m = MemoryManager()  # _redis is None
        # Must not raise when nothing was initialized.
        await m.close()


# ═══════════════════════════════════════════════════════════════════════════
# _redis_op — reconnect-then-fail terminal branch (lines 98-100)
# ═══════════════════════════════════════════════════════════════════════════


class TestRedisOpReconnectFails:
    @pytest.mark.asyncio
    async def test_returns_none_when_reconnect_also_fails(self):
        """If reconnect itself raises, _redis_op swallows it and returns None
        so callers degrade gracefully rather than crashing the request."""
        import redis as _redis_mod

        m = _mk_manager()

        async def always_broken(*_a, **_kw):
            raise _redis_mod.ConnectionError("down")

        always_broken.__name__ = "get"

        with patch("app.memory.redis.from_url", side_effect=RuntimeError("dns fail")):
            result = await m._redis_op(always_broken, "key")

        assert result is None


class _RecordingConn:
    """Async connection that records every SQL string + params it executes."""

    def __init__(self, rowcount=1, rows=None):
        self.calls: list[tuple[str, tuple]] = []
        self._rowcount = rowcount
        self._rows = rows or []

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        result = MagicMock()
        result.rowcount = self._rowcount
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


# ═══════════════════════════════════════════════════════════════════════════
# store_episode — Postgres only, INSERT-ONLY
#
# IMMUTABILITY: `INSERT ... ON CONFLICT (id) DO NOTHING`. These tests ASSERT
# we never issue a DELETE/UPDATE — memory writes are insert-only by project
# mandate — and that no vector is computed or sent anywhere.
# ═══════════════════════════════════════════════════════════════════════════


class TestStoreEpisode:
    @pytest.mark.asyncio
    async def test_inserts_the_row_and_sends_no_vector(self):
        """Storing writes only the Postgres row: summary, keywords, emotion
        tags, importance, conversation and user. No HTTP, no embedding."""
        m = _mk_manager()
        conn = _RecordingConn()

        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            result = await m.store_episode(
                episode_id="ep-1",
                summary="first kiss on the rooftop",
                keywords=["rooftop", "kiss"],
                emotion_tags=["tender"],
                importance=0.9,
                conversation_id="conv-7",
                user_id="alice",
            )

        assert result == "ep-1"
        assert len(conn.calls) == 1
        sql, params = conn.calls[0]
        assert "INSERT INTO companion_episodes" in sql
        assert "embedding_id" not in sql  # there is no vector to point at
        assert params == (
            "ep-1", "conv-7", "first kiss on the rooftop", ["rooftop", "kiss"],
            ["tender"], 0.9, "alice",
        )

    @pytest.mark.asyncio
    async def test_store_is_insert_only_never_deletes(self):
        """SACRED: episode store inserts with ON CONFLICT DO NOTHING and NEVER
        deletes/updates an existing row. This guards the immutability invariant."""
        m = _mk_manager()
        conn = _RecordingConn()

        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            await m.store_episode(
                episode_id="ep-2",
                summary="s",
                keywords=[],
                emotion_tags=[],
                importance=0.5,
            )

        sql, params = conn.calls[0]
        assert "ON CONFLICT (id) DO NOTHING" in sql
        upper = sql.upper()
        assert "DELETE" not in upper
        assert "UPDATE" not in upper
        assert "TRUNCATE" not in upper
        assert params[0] == "ep-2"
        assert params[-1] == "jalsarraf"  # default user

    @pytest.mark.asyncio
    async def test_db_failure_is_logged_and_id_still_returned(self, caplog):
        """A database outage never raises into the extraction/milestone paths."""
        import logging

        m = _mk_manager()

        def broken():
            raise RuntimeError("pg down")

        with patch("app.db.get_conn_autocommit", side_effect=broken), \
             caplog.at_level(logging.ERROR, logger="app.memory"):
            result = await m.store_episode(
                episode_id="ep-4", summary="s", keywords=[], emotion_tags=[],
                importance=0.5,
            )

        assert result == "ep-4"
        assert any("Failed to store episode" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════════════════════
# recall_episodes — Postgres candidates, then Claude Haiku's pick
# ═══════════════════════════════════════════════════════════════════════════


def _episode_row(n, summary, keywords=None, emotion_tags=None, importance=0.5,
                 created_at=None):
    return (
        f"ep-{n}", summary, keywords, emotion_tags, importance,
        created_at or datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc),
    )


class TestRecallEpisodes:
    @pytest.mark.asyncio
    async def test_haiku_pick_sets_order_and_scores(self):
        """Candidates come from Postgres scoped to the user; Haiku's ids pick
        and order them; scores are rank-derived."""
        m = _mk_manager()
        rows = [
            _episode_row(1, "rooftop kiss", ["rooftop"], ["tender"]),
            _episode_row(2, "coffee run"),  # NULL keywords / tags
            _episode_row(3, "rain watch", ["rain"], None),
        ]
        conn = _RecordingConn(rows=rows)
        pick = AsyncMock(return_value={"ids": ["3", "1"]})

        with patch("app.db.get_conn", _conn_ctx(conn)), \
             patch("app.background_llm.json_object", pick):
            results = await m.recall_episodes(
                "what did we do on the roof", limit=3, user_id="bob"
            )

        sql, params = conn.calls[0]
        assert "FROM companion_episodes" in sql and "ts_rank" in sql
        assert params[1] == "bob" and params[2] == 40
        assert pick.await_args.args[0] == "memory_recall_episodes"
        assert results == [
            {"summary": "rain watch", "score": 1.0, "keywords": ["rain"],
             "emotion_tags": []},
            {"summary": "rooftop kiss", "score": 0.5, "keywords": ["rooftop"],
             "emotion_tags": ["tender"]},
        ]

    @pytest.mark.asyncio
    async def test_candidate_text_carries_date_summary_and_keywords(self):
        m = _mk_manager()
        rows = [_episode_row(1, "rooftop kiss", ["rooftop", "night"])]
        conn = _RecordingConn(rows=rows)
        pick = AsyncMock(return_value=None)

        with patch("app.db.get_conn", _conn_ctx(conn)), \
             patch("app.background_llm.json_object", pick):
            await m.recall_episodes("roof", user_id="bob")

        prompt = pick.await_args.args[1]
        assert "[2026-10-20] rooftop kiss (keywords: rooftop, night)" in prompt

    @pytest.mark.asyncio
    async def test_haiku_unavailable_keeps_postgres_order(self):
        m = _mk_manager()
        rows = [_episode_row(n, f"ep {n}", created_at="not-a-date") for n in range(1, 8)]
        conn = _RecordingConn(rows=rows)

        with patch("app.db.get_conn", _conn_ctx(conn)), \
             patch("app.background_llm.json_object", AsyncMock(return_value=None)):
            results = await m.recall_episodes("anything", limit=3)

        assert [r["summary"] for r in results] == ["ep 1", "ep 2", "ep 3"]
        assert [r["score"] for r in results] == [1.0, 0.6667, 0.3333]

    @pytest.mark.asyncio
    async def test_no_candidates_skips_haiku(self):
        m = _mk_manager()
        conn = _RecordingConn(rows=[])
        pick = AsyncMock()

        with patch("app.db.get_conn", _conn_ctx(conn)), \
             patch("app.background_llm.json_object", pick):
            assert await m.recall_episodes("q") == []

        pick.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_database_error_fails_open(self):
        m = _mk_manager()

        def broken():
            raise RuntimeError("pg down")

        with patch("app.db.get_conn", side_effect=broken):
            assert await m.recall_episodes("q") == []


# ═══════════════════════════════════════════════════════════════════════════
# store_fact ttl branch + set_relationship_fact + record_milestone idempotence
# (lines 307, 346, 354) — behavioral gaps not covered by test_memory_session.py
# ═══════════════════════════════════════════════════════════════════════════


class TestFactBranches:
    @pytest.mark.asyncio
    async def test_set_relationship_fact_prefixes_rel_key(self):
        """set_relationship_fact must store under the rel: namespace."""
        m = _mk_manager()
        captured = {}

        async def fake_store_fact(key, value, ttl=None, user_id="jalsarraf"):
            captured["key"] = key
            captured["value"] = value
            captured["user_id"] = user_id

        with patch.object(m, "store_fact", side_effect=fake_store_fact):
            await m.set_relationship_fact("location", "the park", user_id="bob")

        assert captured["key"] == "rel:location"
        assert captured["value"] == "the park"
        assert captured["user_id"] == "bob"

    @pytest.mark.asyncio
    async def test_record_milestone_returns_false_when_already_recorded(self):
        """An already-recorded milestone is NOT overwritten — returns False and
        never re-stores. (Insert-once semantics for milestones.)"""
        m = _mk_manager()
        m.recall_fact = AsyncMock(return_value="2026-01-01")  # already exists
        store = AsyncMock()

        with patch.object(m, "store_fact", store):
            result = await m.record_milestone("first_kiss", user_id="alice")

        assert result is False
        store.assert_not_awaited()  # existing milestone not rewritten


# ═══════════════════════════════════════════════════════════════════════════
# get_milestones — prefix stripping (lines 361-363)
# ═══════════════════════════════════════════════════════════════════════════


class TestGetMilestones:
    @pytest.mark.asyncio
    async def test_strips_full_prefix_into_clean_dict(self):
        m = _mk_manager()
        m.recall_facts_by_pattern = AsyncMock(
            return_value=[
                {"key": "companion:alice:milestone:first_kiss", "value": "2026-01-01"},
                {"key": "companion:alice:milestone:anniversary", "value": "2026-02-14"},
            ]
        )

        result = await m.get_milestones(user_id="alice")

        assert result == {
            "first_kiss": "2026-01-01",
            "anniversary": "2026-02-14",
        }
        # And it queried the milestone pattern for that user.
        m.recall_facts_by_pattern.assert_awaited_once_with(
            "milestone:%", user_id="alice"
        )


# ═══════════════════════════════════════════════════════════════════════════
# recall_for_prompt — parallel fan-out + summary extraction (lines 374-385)
# ═══════════════════════════════════════════════════════════════════════════


class TestRecallForPrompt:
    @pytest.mark.asyncio
    async def test_gathers_three_tiers_and_extracts_summaries(self):
        """Returns (episode summary-strings, facts dict, exchange dicts) and
        passes the user_id through to every tier."""
        m = _mk_manager()
        m.recall_episodes = AsyncMock(
            return_value=[{"summary": "ep one"}, {"summary": "ep two"}]
        )
        m.get_relationship_facts = AsyncMock(return_value={"wearing": "scarf"})
        m.recall_exchanges_with_recency = AsyncMock(
            return_value=[{"user_content": "hi", "assistant_content": "hello"}]
        )

        episodes, facts, exchanges = await m.recall_for_prompt(
            "tell me", user_id="carol"
        )

        assert episodes == ["ep one", "ep two"]  # summaries flattened to strings
        assert facts == {"wearing": "scarf"}
        assert exchanges == [{"user_content": "hi", "assistant_content": "hello"}]
        # user_id propagated to all three tiers
        assert m.recall_episodes.call_args[1]["user_id"] == "carol"
        assert m.get_relationship_facts.call_args[1]["user_id"] == "carol"
        assert m.recall_exchanges_with_recency.call_args[1]["user_id"] == "carol"
        # Default affection_level=0 is threaded so ranking is deterministic.
        assert m.recall_exchanges_with_recency.call_args[1]["affection_level"] == 0

    @pytest.mark.asyncio
    async def test_threads_affection_level_into_exchange_rerank(self):
        """Live chat at high affection must NOT fall into the low-affection
        importance-penalty branch — affection_level has to reach the re-ranker."""
        m = _mk_manager()
        m.recall_episodes = AsyncMock(return_value=[])
        m.get_relationship_facts = AsyncMock(return_value={})
        m.recall_exchanges_with_recency = AsyncMock(return_value=[])

        await m.recall_for_prompt("remember when", user_id="carol", affection_level=9)

        assert m.recall_exchanges_with_recency.call_args[1]["affection_level"] == 9
        assert m.recall_exchanges_with_recency.call_args[1]["user_id"] == "carol"

    @pytest.mark.asyncio
    async def test_failed_facts_and_exchanges_degrade_to_empty(self):
        """return_exceptions keeps a raising tier from reaching the chat path."""
        m = _mk_manager()
        m.recall_episodes = AsyncMock(return_value=[{"summary": "kept"}])
        m.get_relationship_facts = AsyncMock(side_effect=RuntimeError("data down"))
        m.recall_exchanges_with_recency = AsyncMock(side_effect=RuntimeError("pg down"))

        assert await m.recall_for_prompt("hi", user_id="carol") == (["kept"], {}, [])


# ═══════════════════════════════════════════════════════════════════════════
# get_memory_nudge — interval gating + nudge formatting (lines 391-419)
# ═══════════════════════════════════════════════════════════════════════════


class TestGetMemoryNudge:
    @pytest.mark.asyncio
    async def test_none_below_affection_threshold(self):
        """Affection <= 2 never nudges (too cold for reminiscing)."""
        m = _mk_manager()
        m.recall_exchanges_with_recency = AsyncMock()

        assert await m.get_memory_nudge(turn_count=10, affection_level=2) is None
        m.recall_exchanges_with_recency.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_none_when_turn_not_on_interval(self):
        """affection 3-4 nudges every 5 turns — turn 7 is off-interval."""
        m = _mk_manager()
        m.recall_exchanges_with_recency = AsyncMock()

        assert await m.get_memory_nudge(turn_count=7, affection_level=3) is None
        m.recall_exchanges_with_recency.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_none_at_turn_zero(self):
        """turn_count 0 is on every interval mathematically but must NOT nudge."""
        m = _mk_manager()
        m.recall_exchanges_with_recency = AsyncMock()

        assert await m.get_memory_nudge(turn_count=0, affection_level=9) is None
        m.recall_exchanges_with_recency.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_high_affection_uses_interval_three(self):
        """affection >= 7 -> interval 3; turn 6 is on-interval and should query."""
        m = _mk_manager()
        m.recall_exchanges_with_recency = AsyncMock(return_value=[])

        await m.get_memory_nudge(turn_count=6, affection_level=8)

        m.recall_exchanges_with_recency.assert_awaited_once()
        # affection passed through to the recency-weighted recall.
        assert m.recall_exchanges_with_recency.call_args[1]["affection_level"] == 8

    @pytest.mark.asyncio
    async def test_returns_none_when_no_exchanges_found(self):
        """On-interval but empty recall yields no nudge."""
        m = _mk_manager()
        m.recall_exchanges_with_recency = AsyncMock(return_value=[])

        result = await m.get_memory_nudge(turn_count=5, affection_level=4)
        assert result is None

    @pytest.mark.asyncio
    async def test_formats_nudge_from_recalled_exchange(self):
        """On-interval + a hit -> a bracketed [Memory: ...] string quoting both
        sides and listing up to 3 topics."""
        import random as _random

        m = _mk_manager()
        m.recall_exchanges_with_recency = AsyncMock(
            return_value=[
                {
                    "user_content": "I love the rain",
                    "assistant_content": "Then I'll keep an umbrella ready, Commander.",
                    "topics": ["rain", "weather", "umbrella", "extra"],
                }
            ]
        )

        # Freeze random.choice to be deterministic (picks the only/first item).
        with patch.object(_random, "choice", side_effect=lambda seq: seq[0]):
            nudge = await m.get_memory_nudge(turn_count=4, affection_level=5)

        assert nudge is not None
        assert nudge.startswith("[Memory:")
        assert "I love the rain" in nudge
        assert "umbrella ready" in nudge
        # Only the first 3 topics are listed; the 4th is dropped.
        assert "rain, weather, umbrella" in nudge
        assert "extra" not in nudge

    @pytest.mark.asyncio
    async def test_nudge_truncates_long_snippets_to_200_chars(self):
        """Long user/assistant content is clipped to 200 chars in the nudge."""
        import random as _random

        m = _mk_manager()
        long_user = "U" * 500
        long_asst = "A" * 500
        m.recall_exchanges_with_recency = AsyncMock(
            return_value=[
                {
                    "user_content": long_user,
                    "assistant_content": long_asst,
                    "topics": [],
                }
            ]
        )

        with patch.object(_random, "choice", side_effect=lambda seq: seq[0]):
            nudge = await m.get_memory_nudge(turn_count=4, affection_level=5)

        # Each side capped at 200 of its character; "U"*201 must not appear.
        assert "U" * 200 in nudge
        assert "U" * 201 not in nudge
        assert "A" * 200 in nudge
        assert "A" * 201 not in nudge
        # Empty topics -> default phrase.
        assert "a past conversation" in nudge


class TestMilestoneStamp:
    @pytest.mark.asyncio
    async def test_new_milestone_is_stamped_in_aware_utc(self):
        """The app renders milestones in his local time; a naive container
        clock stamp cannot be converted, so it is stored as aware UTC."""
        m = _mk_manager()
        m.recall_fact = AsyncMock(return_value=None)
        m.store_fact = AsyncMock()
        assert await m.record_milestone("first_message", user_id="u1") is True
        key, stamp = m.store_fact.await_args.args
        assert key == "milestone:first_message"
        parsed = datetime.fromisoformat(stamp)
        assert parsed.utcoffset() is not None
        assert parsed.utcoffset().total_seconds() == 0

    @pytest.mark.asyncio
    async def test_existing_milestone_is_not_restamped(self):
        m = _mk_manager()
        m.recall_fact = AsyncMock(return_value="2026-01-01T00:00:00+00:00")
        m.store_fact = AsyncMock()
        assert await m.record_milestone("first_message", user_id="u1") is False
        m.store_fact.assert_not_awaited()
