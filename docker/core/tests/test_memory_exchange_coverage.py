"""Behavioral coverage for app.memory_exchange — cross-user exchange recall.

These functions are bound onto MemoryManager at import time, but each takes an
explicit `self`, so we test them directly with a hand-built fake `self` that
exposes the two attributes they touch (`_http`, `_msg_collection_ready`) plus
an `embed_text` coroutine. The httpx client is a fake recording mock — no
network, no Qdrant.

Behaviors asserted:
- _ensure_msg_collection: short-circuits when ready; creates collection +
  indexes on a 404/miss; tolerant of index-creation failure; warns on HTTP error.
- store_exchange: embeds combined text and PUTs a point scoped to user_id;
  swallows errors.
- recall_exchanges: posts a user-scoped vector search, maps hits; returns []
  on non-200.
- recall_exchanges_with_recency: recency + affection re-ranking and ordering.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# Import app.memory first: app.memory_exchange does `from app.memory import ...`
# at module top, and app.memory calls memory_exchange.attach_to() at the end of
# its own import. Importing app.memory first lets that cycle settle before we
# pull symbols out of memory_exchange (otherwise the partially-initialized
# module has no attach_to yet).
import app.memory  # noqa: F401,E402
from app.memory_exchange import (  # noqa: E402
    _ensure_msg_collection,
    recall_exchanges,
    recall_exchanges_with_recency,
    store_exchange,
)


class _Resp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


def _make_self(get_resp=None, embed_vec=None):
    """Build a fake MemoryManager-like object with an async httpx mock."""
    obj = MagicMock()
    obj._msg_collection_ready = False
    http = MagicMock()
    http.get = AsyncMock(return_value=get_resp or _Resp(200))
    http.put = AsyncMock(return_value=_Resp(200))
    http.post = AsyncMock(return_value=_Resp(200))
    obj._http = http
    obj.embed_text = AsyncMock(return_value=embed_vec or [0.1] * 8)
    return obj


# ── _ensure_msg_collection ───────────────────────────────────────────────────

class TestEnsureMsgCollection:
    @pytest.mark.asyncio
    async def test_short_circuits_when_ready(self):
        obj = _make_self()
        obj._msg_collection_ready = True
        await _ensure_msg_collection(obj)
        obj._http.get.assert_not_called()
        obj._http.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_collection_marks_ready_no_create(self):
        obj = _make_self(get_resp=_Resp(200))
        await _ensure_msg_collection(obj)
        assert obj._msg_collection_ready is True
        obj._http.put.assert_not_called()  # already exists → no PUT

    @pytest.mark.asyncio
    async def test_missing_collection_creates_with_indexes(self):
        obj = _make_self(get_resp=_Resp(404))
        await _ensure_msg_collection(obj)
        assert obj._msg_collection_ready is True
        # First PUT creates collection with the embed dimension; later PUTs are indexes.
        urls = [c.args[0] for c in obj._http.put.call_args_list]
        assert any(u.endswith("/collections/companion_exchanges") for u in urls)
        assert sum(u.endswith("/index") for u in urls) == 2  # topics + user_id

    @pytest.mark.asyncio
    async def test_user_id_index_failure_is_non_critical(self):
        obj = _make_self(get_resp=_Resp(404))

        # First PUT (create) + second PUT (topics index) succeed; the third
        # (user_id index) raises a generic Exception which must be swallowed.
        async def put_side_effect(url, *a, **k):
            if url.endswith("/index") and obj._http.put.call_count == 3:
                raise RuntimeError("index race")
            return _Resp(200)

        obj._http.put = AsyncMock(side_effect=put_side_effect)
        await _ensure_msg_collection(obj)
        assert obj._msg_collection_ready is True  # still ready despite index failure

    @pytest.mark.asyncio
    async def test_get_http_error_then_create(self):
        """GET raising httpx.HTTPError falls through to the create path."""
        obj = _make_self()
        obj._http.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
        await _ensure_msg_collection(obj)
        assert obj._msg_collection_ready is True
        obj._http.put.assert_called()  # proceeded to create

    @pytest.mark.asyncio
    async def test_create_http_error_leaves_not_ready(self):
        obj = _make_self(get_resp=_Resp(404))
        obj._http.put = AsyncMock(side_effect=httpx.ConnectError("down"))
        await _ensure_msg_collection(obj)
        assert obj._msg_collection_ready is False  # creation failed → retry later


# ── store_exchange ───────────────────────────────────────────────────────────

class TestStoreExchange:
    @pytest.mark.asyncio
    async def test_embeds_and_puts_user_scoped_point(self):
        obj = _make_self(embed_vec=[0.5] * 8)
        await store_exchange(
            obj, "ex-1", "hello there", "hi commander",
            topics=["greeting"], mood="warm", importance=0.8,
            conversation_id="conv-1", user_id="alice",
        )
        # embed_text gets the combined Commander/Klukai string.
        combined = obj.embed_text.call_args.args[0]
        assert "Commander: hello there" in combined
        assert "Klukai: hi commander" in combined

        obj._http.put.assert_called_once()
        url, kwargs = obj._http.put.call_args.args[0], obj._http.put.call_args.kwargs
        assert url.endswith("/collections/companion_exchanges/points")
        point = kwargs["json"]["points"][0]
        assert point["id"] == "ex-1"
        assert point["vector"] == [0.5] * 8
        payload = point["payload"]
        assert payload["user_id"] == "alice"
        assert payload["topics"] == ["greeting"]
        assert payload["mood"] == "warm"
        assert payload["importance"] == 0.8
        assert payload["conversation_id"] == "conv-1"

    @pytest.mark.asyncio
    async def test_truncates_long_content_to_500_chars(self):
        obj = _make_self()
        long_user = "U" * 1000   # uppercase letters absent from the label prefixes
        long_asst = "Z" * 1000
        await store_exchange(obj, "ex-2", long_user, long_asst, topics=[])
        combined = obj.embed_text.call_args.args[0]
        # Each side capped at 500 chars in the combined embedding input.
        assert combined.count("U") == 500
        assert combined.count("Z") == 500
        # And the full untruncated content still lands in the stored payload.
        payload = obj._http.put.call_args.kwargs["json"]["points"][0]["payload"]
        assert payload["user_content"] == long_user
        assert payload["assistant_content"] == long_asst

    @pytest.mark.asyncio
    async def test_swallows_errors(self):
        obj = _make_self()
        obj.embed_text = AsyncMock(side_effect=RuntimeError("embed down"))
        # Must not raise — failure is logged and dropped.
        await store_exchange(obj, "ex-3", "x", "y", topics=[])
        obj._http.put.assert_not_called()


# ── recall_exchanges ─────────────────────────────────────────────────────────

def _hit(uc, ac, score, topics=None, mood="composed", created_at="", importance=None):
    payload = {
        "user_content": uc,
        "assistant_content": ac,
        "topics": topics or [],
        "mood": mood,
        "created_at": created_at,
    }
    if importance is not None:
        payload["importance"] = importance
    return {"score": score, "payload": payload}


class TestRecallExchanges:
    @pytest.mark.asyncio
    async def test_maps_hits_and_scopes_to_user(self):
        results = {"result": [_hit("q1", "a1", 0.9, topics=["t"], mood="warm")]}
        obj = _make_self()
        obj.embed_text = AsyncMock(return_value=[0.1, 0.2, 0.3])  # non-zero → recall proceeds
        obj._http.post = AsyncMock(return_value=_Resp(200, results))
        out = await recall_exchanges(obj, "find me", user_id="bob")
        assert len(out) == 1
        assert out[0]["user_content"] == "q1"
        assert out[0]["assistant_content"] == "a1"
        assert out[0]["topics"] == ["t"]
        assert out[0]["mood"] == "warm"
        assert out[0]["score"] == 0.9
        # Search body must filter on the requested user_id.
        body = obj._http.post.call_args.kwargs["json"]
        assert body["filter"]["must"][0]["match"]["value"] == "bob"
        assert body["with_payload"] is True

    @pytest.mark.asyncio
    async def test_projects_stored_importance_from_payload(self):
        """The recall projection must carry the Qdrant-stored importance through
        so the affection-weighted re-rank has a real value to bias on (not the
        0.5 default it falls back to when the key is absent)."""
        results = {"result": [_hit("q1", "a1", 0.9, importance=0.87)]}
        obj = _make_self()
        obj.embed_text = AsyncMock(return_value=[0.1, 0.2, 0.3])
        obj._http.post = AsyncMock(return_value=_Resp(200, results))
        out = await recall_exchanges(obj, "find me")
        assert out[0]["importance"] == 0.87

    @pytest.mark.asyncio
    async def test_importance_defaults_to_half_when_absent(self):
        """A point stored before importance was tracked has no key — recall must
        default it to 0.5 rather than raising or dropping the field."""
        results = {"result": [_hit("q1", "a1", 0.9)]}  # no importance in payload
        obj = _make_self()
        obj.embed_text = AsyncMock(return_value=[0.1, 0.2, 0.3])
        obj._http.post = AsyncMock(return_value=_Resp(200, results))
        out = await recall_exchanges(obj, "find me")
        assert out[0]["importance"] == 0.5

    @pytest.mark.asyncio
    async def test_non_200_returns_empty(self):
        obj = _make_self()
        obj._http.post = AsyncMock(return_value=_Resp(500, text="qdrant boom"))
        out = await recall_exchanges(obj, "q")
        assert out == []

    @pytest.mark.asyncio
    async def test_empty_result_returns_empty(self):
        obj = _make_self()
        obj._http.post = AsyncMock(return_value=_Resp(200, {"result": []}))
        out = await recall_exchanges(obj, "q")
        assert out == []


# ── recall_exchanges_with_recency ────────────────────────────────────────────
#
# recall_exchanges_with_recency calls `self.recall_exchanges(...)` (the bound
# method), so we stub it on the instance via an AsyncMock rather than patching
# the module-level function.

def _self_with_recall(rows):
    obj = _make_self()
    obj.recall_exchanges = AsyncMock(return_value=rows)
    return obj


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
        # bound-method call: self.recall_exchanges(query, limit=10, user_id="bob")
        assert obj.recall_exchanges.call_args.kwargs["limit"] == 10
        assert obj.recall_exchanges.call_args.kwargs["user_id"] == "bob"

    @pytest.mark.asyncio
    async def test_recent_high_score_outranks_old_high_score(self):
        now = datetime.now()
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
    async def test_truncates_to_limit_after_rerank(self):
        now = datetime.now().isoformat()
        rows = [
            {"user_content": f"u{i}", "assistant_content": "a", "topics": [],
             "mood": "composed", "score": 0.5 + i * 0.01, "created_at": now}
            for i in range(6)
        ]
        obj = _self_with_recall(rows)
        out = await recall_exchanges_with_recency(obj, "q", limit=3)
        assert len(out) == 3  # trimmed to requested limit

    @pytest.mark.asyncio
    async def test_bad_created_at_defaults_to_30_days(self):
        obj = _self_with_recall([{
            "user_content": "x", "assistant_content": "y",
            "topics": [], "mood": "composed", "score": 0.5,
            "created_at": "not-a-date",
        }])
        # affection_level=4 sits between the <=2 and >=6 bias bands, so no
        # importance bias is applied — isolating the recency math.
        out = await recall_exchanges_with_recency(obj, "q", limit=1, affection_level=4)
        # days_ago=30 → recency_factor = 1/31 ≈ 0.0323; final score blends it in.
        expected = (1 - 0.15) * 0.5 + 0.15 * (1.0 / 31.0)
        assert out[0]["final_score"] == pytest.approx(expected, abs=1e-6)

    @pytest.mark.asyncio
    async def test_high_affection_biases_toward_importance(self):
        now = datetime.now().isoformat()
        row = {
            "user_content": "imp", "assistant_content": "a",
            "topics": [], "mood": "composed", "score": 0.5,
            "importance": 1.0, "created_at": now,
        }
        high = await recall_exchanges_with_recency(
            _self_with_recall([dict(row)]), "q", limit=1, affection_level=9)
        neutral = await recall_exchanges_with_recency(
            _self_with_recall([dict(row)]), "q", limit=1, affection_level=4)
        # affection>=6 adds importance*0.2 to the final score.
        assert high[0]["final_score"] == pytest.approx(neutral[0]["final_score"] + 0.2, abs=1e-6)

    @pytest.mark.asyncio
    async def test_low_affection_biases_toward_unimportant(self):
        now = datetime.now().isoformat()
        obj = _self_with_recall([{
            "user_content": "x", "assistant_content": "y",
            "topics": [], "mood": "composed", "score": 0.5,
            "importance": 0.0, "created_at": now,
        }])
        out = await recall_exchanges_with_recency(obj, "q", limit=1, affection_level=1)
        # affection<=2 adds (1.0 - importance)*0.1 = 0.1 for importance 0.0.
        base = (1 - 0.15) * 0.5 + 0.15 * 1.0  # created_at ~now → recency_factor≈1
        assert out[0]["final_score"] == pytest.approx(base + 0.1, abs=1e-3)

    @pytest.mark.asyncio
    async def test_stored_importance_flows_through_real_recall_and_changes_order(self):
        """End-to-end: the recency re-rank must read the IMPORTANCE that the real
        recall_exchanges projection pulls from the Qdrant payload — not a constant
        0.5. Two same-age, same-score hits with different stored importance must
        get different final_score contributions, and at high affection the more
        important one must sort first even though it was listed second.
        """
        now = datetime.now().isoformat()
        # Same recency + same base score → recency math is identical, so any
        # ordering difference comes purely from the importance bias.
        results = {"result": [
            _hit("low", "a", 0.7, created_at=now, importance=0.1),
            _hit("high", "a", 0.7, created_at=now, importance=0.9),
        ]}
        obj = _make_self()
        obj.embed_text = AsyncMock(return_value=[0.1, 0.2, 0.3])
        obj._http.post = AsyncMock(return_value=_Resp(200, results))
        # Bind the REAL recall_exchanges so the re-rank consumes its actual
        # projection (the bug lived in that projection, not the re-rank math).
        import types
        obj.recall_exchanges = types.MethodType(recall_exchanges, obj)
        out = await recall_exchanges_with_recency(obj, "q", limit=2, affection_level=9)

        by_uc = {e["user_content"]: e for e in out}
        # The bias is importance*0.2: 0.9 vs 0.1 → a 0.16 gap in final_score.
        assert by_uc["high"]["final_score"] == pytest.approx(
            by_uc["low"]["final_score"] + (0.9 - 0.1) * 0.2, abs=1e-6
        )
        # And that real differentiation flips the order: high importance wins.
        assert [e["user_content"] for e in out] == ["high", "low"]


# ── store_exchange: status-check + Postgres fallback (SACRED, 2026-06-11) ────
#
# httpx does NOT raise on 4xx/5xx — the Qdrant PUT must be status-checked, and
# on ANY Qdrant/embed failure the exchange must land in the companion_exchanges
# Postgres fallback (mirroring the episodes fallback in app/memory.py) instead
# of being stored NOWHERE.


class _RecordingConn:
    """Async connection recording every SQL string + params it executes."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return MagicMock()

    async def commit(self):
        pass

    async def rollback(self):
        pass


def _conn_ctx(conn):
    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


class TestStoreExchangePGFallback:
    @pytest.mark.asyncio
    async def test_qdrant_error_status_falls_back_to_pg(self):
        obj = _make_self()
        obj._http.put = AsyncMock(return_value=_Resp(500, text="qdrant down"))
        conn = _RecordingConn()
        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            await store_exchange(
                obj, "ex-9", "hello", "hi there", topics=["t"], mood="warm",
                importance=0.7, conversation_id="conv-9", user_id="alice",
            )
        assert len(conn.calls) == 1
        sql, params = conn.calls[0]
        assert "INSERT INTO companion_exchanges" in sql
        assert "ON CONFLICT (id) DO NOTHING" in sql
        # Insert-only — never destructive on a memory path.
        assert "DELETE" not in sql.upper() and "UPDATE" not in sql.upper()
        assert params[0] == "ex-9"
        assert "hello" in params and "hi there" in params
        assert params[-1] == "alice"

    @pytest.mark.asyncio
    async def test_embed_failure_still_persists_to_pg(self):
        """Embedding outage must not lose the exchange — the raw text pair
        still lands in Postgres (a later job can re-vectorize)."""
        obj = _make_self()
        obj.embed_text = AsyncMock(side_effect=RuntimeError("embed down"))
        conn = _RecordingConn()
        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            await store_exchange(obj, "ex-10", "u-text", "a-text", topics=[])
        obj._http.put.assert_not_called()
        assert len(conn.calls) == 1
        assert "INSERT INTO companion_exchanges" in conn.calls[0][0]

    @pytest.mark.asyncio
    async def test_qdrant_success_skips_pg_fallback(self):
        obj = _make_self()
        conn = _RecordingConn()
        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            await store_exchange(obj, "ex-11", "u", "a", topics=[])
        obj._http.put.assert_called_once()
        assert conn.calls == []  # primary store succeeded → no fallback write

    @pytest.mark.asyncio
    async def test_both_stores_failing_is_swallowed(self):
        obj = _make_self()
        obj._http.put = AsyncMock(side_effect=RuntimeError("qdrant down"))

        def broken():
            raise RuntimeError("pg down")

        with patch("app.db.get_conn_autocommit", side_effect=broken):
            # Must not raise — chat path stays alive even on double failure.
            await store_exchange(obj, "ex-12", "u", "a", topics=[])
