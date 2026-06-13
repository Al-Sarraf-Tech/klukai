"""Tests for MemoryManager — session state + fact storage branches.

Focus on pure async-method logic with Redis/HTTP mocked out.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _mk_manager():
    """Construct a MemoryManager with Redis + HTTP mocked."""
    from app.memory import MemoryManager
    m = MemoryManager()
    m._redis = AsyncMock()
    m._http = AsyncMock()
    m._redis_available = True
    return m


# ═══════════════════════════════════════════════════════════════════════════
# _redis_op — transparent wrapper with fail-safe
# ═══════════════════════════════════════════════════════════════════════════


class TestRedisOp:
    @pytest.mark.asyncio
    async def test_delegates_to_op(self):
        m = _mk_manager()
        fake_op = AsyncMock(return_value="result-value")
        result = await m._redis_op(fake_op, "arg1", kw="v")
        assert result == "result-value"
        fake_op.assert_awaited_once_with("arg1", kw="v")

    @pytest.mark.asyncio
    async def test_reconnects_on_connection_error(self):
        """Only redis.ConnectionError/TimeoutError are caught — others propagate."""
        import redis as _redis_mod

        m = _mk_manager()
        call_count = {"n": 0}

        async def flaky(*_a, **_kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _redis_mod.ConnectionError("broken")
            return "recovered"
        flaky.__name__ = "get"  # _redis_op calls getattr(self._redis, op.__name__)

        with patch("app.memory.redis.from_url") as fake_from_url:
            new_redis = AsyncMock()
            new_redis.get = AsyncMock(return_value="recovered")
            fake_from_url.return_value = new_redis
            result = await m._redis_op(flaky, "key")

        assert result == "recovered"


# ═══════════════════════════════════════════════════════════════════════════
# Session state (Tier 1, Redis)
# ═══════════════════════════════════════════════════════════════════════════


class TestGetSession:
    @pytest.mark.asyncio
    async def test_returns_none_when_missing(self):
        m = _mk_manager()
        m._redis.get = AsyncMock(return_value=None)
        result = await m.get_session("conv-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_parses_stored_state(self):
        from app.models import SessionState
        m = _mk_manager()

        state = SessionState(conversation_id="conv-1")
        state.turns = [{"role": "user", "content": "hi"}]
        m._redis.get = AsyncMock(return_value=state.model_dump_json())

        result = await m.get_session("conv-1")
        assert result is not None
        assert result.conversation_id == "conv-1"
        assert len(result.turns) == 1

    @pytest.mark.asyncio
    async def test_returns_none_on_corrupt_json(self):
        m = _mk_manager()
        m._redis.get = AsyncMock(return_value="not-json-at-all")
        result = await m.get_session("conv-1")
        assert result is None


class TestSaveSession:
    @pytest.mark.asyncio
    async def test_saves_with_ttl(self):
        from app.memory import SESSION_TTL
        from app.models import SessionState

        m = _mk_manager()
        m._redis.set = AsyncMock()

        state = SessionState(conversation_id="conv-1")
        state.turns = [{"role": "user", "content": "hi"}]

        await m.save_session("conv-1", state)

        m._redis.set.assert_awaited_once()
        args, kwargs = m._redis.set.call_args
        assert "companion:session:conv-1" in args[0]
        assert kwargs.get("ex") == SESSION_TTL

    @pytest.mark.asyncio
    async def test_trims_oversized_turns_to_max(self):
        from app.memory import MAX_SESSION_TURNS
        from app.models import SessionState

        m = _mk_manager()
        m._redis.set = AsyncMock()

        state = SessionState(conversation_id="conv-1")
        state.turns = [
            {"role": "user", "content": f"t{i}"}
            for i in range(MAX_SESSION_TURNS + 10)
        ]

        await m.save_session("conv-1", state)

        # state mutated in place — old turns trimmed
        assert len(state.turns) == MAX_SESSION_TURNS

    @pytest.mark.asyncio
    async def test_updates_last_activity(self):
        from datetime import datetime
        from app.models import SessionState

        m = _mk_manager()
        m._redis.set = AsyncMock()

        state = SessionState(conversation_id="conv-1")
        before = datetime.now()
        await m.save_session("conv-1", state)

        assert state.last_activity >= before


class TestAddTurn:
    @pytest.mark.asyncio
    async def test_appends_and_saves(self):
        from app.models import SessionState

        m = _mk_manager()
        m._redis.set = AsyncMock()

        state = SessionState(conversation_id="c")
        state.turn_count = 3

        result = await m.add_turn("c", "user", "hello", state)

        assert result is state
        assert state.turns[-1] == {"role": "user", "content": "hello"}
        assert state.turn_count == 4
        m._redis.set.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
# Fact storage via DATA_URL HTTP calls
# ═══════════════════════════════════════════════════════════════════════════


class TestFacts:
    @pytest.mark.asyncio
    async def test_store_fact_calls_data_api(self):
        m = _mk_manager()
        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock()
        m._http.post = AsyncMock(return_value=fake_resp)

        await m.store_fact("rel:wearing", "blue scarf", user_id="alice")

        m._http.post.assert_awaited_once()
        url = m._http.post.call_args[0][0]
        assert "memory/store" in url or "memory" in url
        body = m._http.post.call_args[1]["json"]
        assert body["key"] == "companion:alice:rel:wearing"
        assert body["value"] == "blue scarf"

    @pytest.mark.asyncio
    async def test_store_fact_swallows_http_errors(self):
        m = _mk_manager()

        async def broken(*_a, **_kw):
            raise RuntimeError("data api down")

        m._http.post = broken
        # Should not raise
        await m.store_fact("rel:k", "v")

    @pytest.mark.asyncio
    async def test_recall_fact_returns_value(self):
        m = _mk_manager()
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json = MagicMock(return_value={"found": True, "value": "blue scarf"})
        m._http.get = AsyncMock(return_value=fake_resp)

        v = await m.recall_fact("rel:wearing", user_id="alice")
        assert v == "blue scarf"

    @pytest.mark.asyncio
    async def test_recall_fact_returns_none_when_not_found(self):
        m = _mk_manager()
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json = MagicMock(return_value={"found": False})
        m._http.get = AsyncMock(return_value=fake_resp)

        v = await m.recall_fact("rel:wearing")
        assert v is None

    @pytest.mark.asyncio
    async def test_recall_fact_swallows_errors(self):
        m = _mk_manager()

        async def broken(*_a, **_kw):
            raise RuntimeError("data api broken")

        m._http.get = broken
        v = await m.recall_fact("rel:x")
        assert v is None

    @pytest.mark.asyncio
    async def test_recall_facts_by_pattern_passes_prefixed_pattern(self):
        """Pattern is forwarded to data API prefixed with companion:{user}:."""
        m = _mk_manager()
        fake_resp = MagicMock()
        fake_resp.json = MagicMock(return_value={"entries": [
            {"key": "companion:alice:rel:wearing", "value": "scarf"},
            {"key": "companion:alice:rel:location", "value": "park"},
        ]})
        m._http.get = AsyncMock(return_value=fake_resp)

        facts = await m.recall_facts_by_pattern("rel:%", user_id="alice")

        assert len(facts) == 2
        params = m._http.get.call_args[1]["params"]
        assert params["pattern"] == "companion:alice:rel:%"

    @pytest.mark.asyncio
    async def test_get_relationship_facts_strips_prefix(self):
        """get_relationship_facts returns a {key: value} dict with prefix stripped."""
        m = _mk_manager()
        fake_resp = MagicMock()
        fake_resp.json = MagicMock(return_value={"entries": [
            {"key": "companion:alice:rel:wearing", "value": "scarf"},
            {"key": "companion:alice:rel:location", "value": "park"},
        ]})
        m._http.get = AsyncMock(return_value=fake_resp)

        d = await m.get_relationship_facts(user_id="alice")
        assert d == {"wearing": "scarf", "location": "park"}


# ═══════════════════════════════════════════════════════════════════════════
# Milestones
# ═══════════════════════════════════════════════════════════════════════════


class TestMilestones:
    @pytest.mark.asyncio
    async def test_record_milestone_calls_store_fact(self):
        m = _mk_manager()

        captured = {}

        async def fake_store_fact(key, value, ttl=None, user_id="jalsarraf"):
            captured["key"] = key
            captured["user_id"] = user_id

        with patch.object(m, "store_fact", side_effect=fake_store_fact):
            result = await m.record_milestone("first_message", user_id="alice")

        assert result is True
        assert "milestone" in captured["key"]
        assert captured["user_id"] == "alice"
