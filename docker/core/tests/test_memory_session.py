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
    """Construct a MemoryManager with Redis mocked."""
    from app.memory import MemoryManager

    m = MemoryManager()
    m._redis = AsyncMock()
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
    async def test_rejects_corrupt_session_without_replacing_it(self):
        m = _mk_manager()
        m._redis.get = AsyncMock(return_value="not-json-at-all")
        from app.memory import SessionUnavailable

        with pytest.raises(SessionUnavailable, match="invalid"):
            await m.get_session("conv-1")


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
            {"role": "user", "content": f"t{i}"} for i in range(MAX_SESSION_TURNS + 10)
        ]

        await m.save_session("conv-1", state)

        # Past the limit, a whole chunk drops. The prefix does not slide by one.
        from app.conversation_continuity import HISTORY_CHUNK

        assert len(state.turns) == MAX_SESSION_TURNS + 10 - HISTORY_CHUNK
        assert state.turns[0]["content"] == f"t{HISTORY_CHUNK}"

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


# Fact storage is covered by test_memory_facts.py (app/fact_store.py).


# ═══════════════════════════════════════════════════════════════════════════
# Milestones
# ═══════════════════════════════════════════════════════════════════════════


class TestMilestones:
    @pytest.mark.asyncio
    async def test_record_milestone_inserts_under_the_users_key(self):
        m = _mk_manager()
        insert = AsyncMock(return_value=True)
        with patch("app.memory_facts.fact_store.insert_if_absent", insert):
            result = await m.record_milestone("first_message", user_id="alice")
        assert result is True
        assert insert.await_args.args[0] == "companion:alice:milestone:first_message"
