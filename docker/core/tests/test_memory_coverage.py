"""Behavioral coverage for app/memory.py — episodic (Qdrant) + embedding +
combined-recall + nudge tiers.

Complements tests/test_memory_session.py (which covers session/fact/milestone
record). Here we exercise the embedding cache path, Qdrant collection
bootstrap, episode store (incl. the insert-only PG fallback), episode recall
scoring, the parallel recall_for_prompt fan-out, and the memory-nudge
interval/formatting logic.

Every test asserts concrete behavior: what HTTP body was sent, what got
stored, what got returned, and — for the compaction-adjacent store path —
that we INSERT-ONLY and never DELETE existing vectors/rows.

All I/O is mocked; datetime/random are frozen for determinism.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _mk_manager():
    """MemoryManager with Redis + HTTP mocked (no live services)."""
    from app.memory import MemoryManager

    m = MemoryManager()
    m._redis = AsyncMock()
    m._http = AsyncMock()
    return m


class _Resp:
    """Minimal httpx.Response stand-in with controllable status/json/text."""

    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("err", request=None, response=None)


# ═══════════════════════════════════════════════════════════════════════════
# init / close — lifecycle wiring
# ═══════════════════════════════════════════════════════════════════════════


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_init_builds_clients_and_ensures_collections(self):
        """init() must create Redis + HTTP clients and bootstrap BOTH collections."""
        from app.memory import MemoryManager

        m = MemoryManager()
        fake_redis = MagicMock()
        fake_http = MagicMock()

        with patch("app.memory.redis.from_url", return_value=fake_redis) as from_url, \
             patch("app.memory.httpx.AsyncClient", return_value=fake_http), \
             patch.object(MemoryManager, "_ensure_qdrant_collection", AsyncMock()) as ensure_ep, \
             patch.object(MemoryManager, "_ensure_msg_collection", AsyncMock()) as ensure_msg:
            await m.init()

        assert m._redis is fake_redis
        assert m._http is fake_http
        from_url.assert_called_once()
        ensure_ep.assert_awaited_once()
        ensure_msg.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_closes_both_clients(self):
        m = _mk_manager()
        m._redis.aclose = AsyncMock()
        m._http.aclose = AsyncMock()

        await m.close()

        m._redis.aclose.assert_awaited_once()
        m._http.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_is_safe_when_clients_unset(self):
        from app.memory import MemoryManager

        m = MemoryManager()  # _redis and _http are None
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


# ═══════════════════════════════════════════════════════════════════════════
# _ensure_qdrant_collection — bootstrap branches (lines 136-166)
# ═══════════════════════════════════════════════════════════════════════════


class TestEnsureQdrantCollection:
    @pytest.mark.asyncio
    async def test_noop_when_already_ready(self):
        """Idempotent: once ready, it must not touch HTTP again."""
        m = _mk_manager()
        m._collection_ready = True
        m._http.get = AsyncMock()

        await m._ensure_qdrant_collection()

        m._http.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_marks_ready_when_collection_exists(self):
        """A 200 GET means the collection exists — mark ready, don't recreate."""
        m = _mk_manager()
        m._http.get = AsyncMock(return_value=_Resp(200))
        m._http.put = AsyncMock()

        await m._ensure_qdrant_collection()

        assert m._collection_ready is True
        m._http.put.assert_not_awaited()  # no create when it already exists

    @pytest.mark.asyncio
    async def test_creates_collection_with_correct_vector_config(self):
        """A 404 GET triggers a PUT creating a 768-dim Cosine collection,
        followed by a keyword index on user_id for filtered search."""
        from app.memory import COLLECTION_NAME, EMBED_DIM

        m = _mk_manager()
        m._http.get = AsyncMock(return_value=_Resp(404))
        m._http.put = AsyncMock(return_value=_Resp(200))

        await m._ensure_qdrant_collection()

        assert m._collection_ready is True
        # First PUT = create collection with the expected vector params.
        create_call = m._http.put.await_args_list[0]
        assert COLLECTION_NAME in create_call[0][0]
        vectors = create_call[1]["json"]["vectors"]
        assert vectors["size"] == EMBED_DIM
        assert vectors["distance"] == "Cosine"
        # Second PUT = the user_id keyword index.
        index_call = m._http.put.await_args_list[1]
        assert index_call[1]["json"]["field_name"] == "user_id"
        assert index_call[1]["json"]["field_schema"] == "keyword"

    @pytest.mark.asyncio
    async def test_index_failure_does_not_block_readiness(self):
        """Index creation is non-critical: if it fails, the collection is still ready."""
        m = _mk_manager()
        m._http.get = AsyncMock(return_value=_Resp(404))

        calls = {"n": 0}

        async def put(*_a, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return _Resp(200)  # create succeeds
            raise RuntimeError("index boom")  # index fails

        m._http.put = put
        await m._ensure_qdrant_collection()

        assert m._collection_ready is True

    @pytest.mark.asyncio
    async def test_create_http_error_leaves_not_ready(self):
        """If creation itself fails, stay not-ready so a later call retries."""
        import httpx

        m = _mk_manager()
        m._http.get = AsyncMock(side_effect=httpx.ConnectError("no qdrant"))
        m._http.put = AsyncMock(side_effect=httpx.ConnectError("no qdrant"))

        await m._ensure_qdrant_collection()

        assert m._collection_ready is False


# ═══════════════════════════════════════════════════════════════════════════
# embed_text — cache-first + service call + failure sentinel (lines 177-201)
# ═══════════════════════════════════════════════════════════════════════════


class TestEmbedText:
    @pytest.mark.asyncio
    async def test_returns_cached_vector_without_http(self):
        """Cache hit short-circuits — no embedding service call."""
        m = _mk_manager()
        m._http.post = AsyncMock()
        cached = [0.1] * 768

        with patch("app.caches.get_embedding", AsyncMock(return_value=cached)):
            vec = await m.embed_text("hello")

        assert vec == cached
        m._http.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_calls_service_on_cache_miss_and_writes_through(self):
        """Cache miss -> POST to inference -> parse data[0].embedding -> cache it."""
        m = _mk_manager()
        service_vec = [0.5] * 768
        m._http.post = AsyncMock(
            return_value=_Resp(200, {"data": [{"embedding": service_vec}]})
        )
        put = AsyncMock()

        with patch("app.caches.get_embedding", AsyncMock(return_value=None)), \
             patch("app.caches.put_embedding", put):
            vec = await m.embed_text("fresh text")

        assert vec == service_vec
        # POST body must carry the input text + embed model.
        body = m._http.post.call_args[1]["json"]
        assert body["input"] == "fresh text"
        assert body["model"] == "nomic-embed-text-v1.5"
        # Write-through cache populated with the new vector.
        put.assert_awaited_once_with("fresh text", service_vec)

    @pytest.mark.asyncio
    async def test_returns_zero_vector_on_failure_by_default(self):
        """Search paths degrade quietly: failure -> EMBED_DIM-length zero vector."""
        from app.memory import EMBED_DIM, MemoryManager

        m = _mk_manager()
        m._http.post = AsyncMock(side_effect=RuntimeError("inference down"))

        with patch("app.caches.get_embedding", AsyncMock(return_value=None)):
            vec = await m.embed_text("x")

        assert vec == [0.0] * EMBED_DIM
        assert MemoryManager.is_zero_vector(vec) is True

    @pytest.mark.asyncio
    async def test_raises_embedding_failed_when_requested(self):
        """Store paths pass raise_on_failure=True to avoid indexing a zero vector."""
        from app.memory import EmbeddingFailed

        m = _mk_manager()
        m._http.post = AsyncMock(side_effect=RuntimeError("inference down"))

        with patch("app.caches.get_embedding", AsyncMock(return_value=None)):
            with pytest.raises(EmbeddingFailed):
                await m.embed_text("x", raise_on_failure=True)

    @pytest.mark.asyncio
    async def test_cache_read_error_falls_through_to_service(self):
        """A broken cache must not break embedding — fall through to the service."""
        m = _mk_manager()
        service_vec = [0.25] * 768
        m._http.post = AsyncMock(
            return_value=_Resp(200, {"data": [{"embedding": service_vec}]})
        )

        with patch("app.caches.get_embedding", AsyncMock(side_effect=RuntimeError("redis"))), \
             patch("app.caches.put_embedding", AsyncMock(side_effect=RuntimeError("redis"))):
            vec = await m.embed_text("text")

        assert vec == service_vec  # cache write failure also swallowed


class TestIsZeroVector:
    def test_detects_all_zero(self):
        from app.memory import MemoryManager

        assert MemoryManager.is_zero_vector([0.0, 0.0, 0.0]) is True

    def test_rejects_nonzero(self):
        from app.memory import MemoryManager

        assert MemoryManager.is_zero_vector([0.0, 0.1, 0.0]) is False


# ═══════════════════════════════════════════════════════════════════════════
# store_episode — Qdrant upsert + INSERT-ONLY PG fallback (lines 218-261)
#
# IMMUTABILITY: the PG fallback is `INSERT ... ON CONFLICT (id) DO NOTHING`.
# These tests ASSERT we never issue a DELETE/UPDATE — memory writes are
# insert-only by project mandate.
# ═══════════════════════════════════════════════════════════════════════════


class _RecordingConn:
    """Async connection that records every SQL string + params it executes."""

    def __init__(self, rowcount=1):
        self.calls: list[tuple[str, tuple]] = []
        self._rowcount = rowcount

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        result = MagicMock()
        result.rowcount = self._rowcount
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


class TestStoreEpisode:
    @pytest.mark.asyncio
    async def test_upserts_to_qdrant_with_full_payload(self):
        """The vector PUT must carry id, embedding, and the full metadata payload."""
        m = _mk_manager()
        m.embed_text = AsyncMock(return_value=[0.3] * 768)
        m._http.put = AsyncMock(return_value=_Resp(200))
        conn = _RecordingConn()

        frozen = datetime(2026, 5, 25, 12, 0, 0)
        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)), \
             patch("app.memory.datetime") as dt:
            dt.now.return_value = frozen
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
        body = m._http.put.call_args[1]["json"]
        point = body["points"][0]
        assert point["id"] == "ep-1"
        assert point["vector"] == [0.3] * 768
        payload = point["payload"]
        assert payload["summary"] == "first kiss on the rooftop"
        assert payload["keywords"] == ["rooftop", "kiss"]
        assert payload["emotion_tags"] == ["tender"]
        assert payload["importance"] == 0.9
        assert payload["conversation_id"] == "conv-7"
        assert payload["user_id"] == "alice"
        assert payload["created_at"] == frozen.isoformat()

    @pytest.mark.asyncio
    async def test_pg_fallback_is_insert_only_never_deletes(self):
        """SACRED: episode store inserts with ON CONFLICT DO NOTHING and NEVER
        deletes/updates an existing row. This guards the immutability invariant."""
        m = _mk_manager()
        m.embed_text = AsyncMock(return_value=[0.1] * 768)
        m._http.put = AsyncMock(return_value=_Resp(200))
        conn = _RecordingConn()

        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            await m.store_episode(
                episode_id="ep-2",
                summary="s",
                keywords=[],
                emotion_tags=[],
                importance=0.5,
            )

        assert len(conn.calls) == 1
        sql, params = conn.calls[0]
        assert "INSERT INTO companion_episodes" in sql
        assert "ON CONFLICT (id) DO NOTHING" in sql
        # Immutability invariant — no destructive SQL on the memory store path.
        upper = sql.upper()
        assert "DELETE" not in upper
        assert "UPDATE" not in upper
        assert "TRUNCATE" not in upper
        # Episode id is also used as the embedding_id (positions 0 and 6).
        assert params[0] == "ep-2"
        assert params[6] == "ep-2"

    @pytest.mark.asyncio
    async def test_qdrant_failure_still_persists_to_pg(self):
        """Qdrant outage must not lose the episode — PG fallback still runs."""
        m = _mk_manager()
        m.embed_text = AsyncMock(return_value=[0.1] * 768)
        m._http.put = AsyncMock(side_effect=RuntimeError("qdrant down"))
        conn = _RecordingConn()

        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            result = await m.store_episode(
                episode_id="ep-3", summary="s", keywords=[], emotion_tags=[],
                importance=0.5,
            )

        assert result == "ep-3"  # returns id despite Qdrant failure
        assert len(conn.calls) == 1
        assert "INSERT INTO companion_episodes" in conn.calls[0][0]

    @pytest.mark.asyncio
    async def test_pg_failure_is_swallowed_returns_id(self):
        """If BOTH stores fail the id is still returned (best-effort store)."""
        m = _mk_manager()
        m.embed_text = AsyncMock(return_value=[0.1] * 768)
        m._http.put = AsyncMock(side_effect=RuntimeError("qdrant down"))

        def broken():
            raise RuntimeError("pg down")

        with patch("app.db.get_conn_autocommit", side_effect=broken):
            result = await m.store_episode(
                episode_id="ep-4", summary="s", keywords=[], emotion_tags=[],
                importance=0.5,
            )

        assert result == "ep-4"


# ═══════════════════════════════════════════════════════════════════════════
# recall_episodes — vector search request + result shaping (lines 267-284)
# ═══════════════════════════════════════════════════════════════════════════


class TestRecallEpisodes:
    @pytest.mark.asyncio
    async def test_builds_user_scoped_search_and_shapes_hits(self):
        """Search must be user-filtered + threshold-bounded; hits map to dicts
        carrying summary/score/keywords/emotion_tags."""
        m = _mk_manager()
        m.embed_text = AsyncMock(return_value=[0.4] * 768)
        hits = {
            "result": [
                {
                    "score": 0.91,
                    "payload": {
                        "summary": "rooftop kiss",
                        "keywords": ["rooftop"],
                        "emotion_tags": ["tender"],
                    },
                },
                {
                    "score": 0.55,
                    "payload": {"summary": "coffee run"},  # missing optional fields
                },
            ]
        }
        m._http.post = AsyncMock(return_value=_Resp(200, hits))

        results = await m.recall_episodes(
            "what did we do", limit=3, min_score=0.4, user_id="bob"
        )

        # Request shape
        body = m._http.post.call_args[1]["json"]
        assert body["vector"] == [0.4] * 768
        assert body["limit"] == 3
        assert body["score_threshold"] == 0.4
        assert body["filter"]["must"][0]["match"]["value"] == "bob"
        # Result shape
        assert len(results) == 2
        assert results[0] == {
            "summary": "rooftop kiss",
            "score": 0.91,
            "keywords": ["rooftop"],
            "emotion_tags": ["tender"],
        }
        # Missing optional payload fields default to empty lists.
        assert results[1]["summary"] == "coffee run"
        assert results[1]["keywords"] == []
        assert results[1]["emotion_tags"] == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_non_200(self):
        """A failed Qdrant search returns [] rather than raising."""
        m = _mk_manager()
        m.embed_text = AsyncMock(return_value=[0.0] * 768)
        m._http.post = AsyncMock(return_value=_Resp(500, text="boom"))

        results = await m.recall_episodes("q")
        assert results == []


# ═══════════════════════════════════════════════════════════════════════════
# store_fact ttl branch + set_relationship_fact + record_milestone idempotence
# (lines 307, 346, 354) — behavioral gaps not covered by test_memory_session.py
# ═══════════════════════════════════════════════════════════════════════════


class TestFactBranches:
    @pytest.mark.asyncio
    async def test_store_fact_includes_ttl_when_given(self):
        """A ttl arg must be forwarded as ttl_seconds in the data-API body."""
        m = _mk_manager()
        m._http.post = AsyncMock(return_value=_Resp(200))

        await m.store_fact("rel:wearing", "scarf", ttl=3600, user_id="alice")

        body = m._http.post.call_args[1]["json"]
        assert body["ttl_seconds"] == 3600
        assert body["key"] == "companion:alice:rel:wearing"
        assert body["value"] == "scarf"

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
