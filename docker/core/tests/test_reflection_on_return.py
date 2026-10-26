"""Tests for _maybe_reflect_on_return — returns-after-absence greeting."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _FakeConn:
    def __init__(self, *batches):
        self._batches = list(batches)

    async def execute(self, sql, params=None):
        result = AsyncMock()
        if self._batches:
            batch = self._batches.pop(0)
            # First call returns single row via fetchone; rest via fetchall
            if isinstance(batch, tuple):
                result.fetchone = AsyncMock(return_value=batch)
            else:
                result.fetchall = AsyncMock(return_value=batch)
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _FakePool:
    def __init__(self, *batch_sequences):
        self._sequences = list(batch_sequences)

    def connection(self):
        seq = self._sequences.pop(0) if self._sequences else []
        return _FakeConn(*seq)


def _mk_affection(level: int = 5) -> SimpleNamespace:
    return SimpleNamespace(level=level, level_name="Trust", score=500,
                           consecutive_days=1, total_interactions=10,
                           first_interaction=datetime(2026, 1, 1, tzinfo=timezone.utc))


_HAIKU = "app.extraction_llm.complete"


class TestReflectionOnReturn:
    @pytest.mark.asyncio
    async def test_skips_when_no_last_message(self):
        """Brand new user — no prior message — should not greet."""
        from app.chat import _maybe_reflect_on_return

        pool = _FakePool([
            (None,),  # MAX(created_at) returns NULL
        ])

        with patch("app.db.get_pool", return_value=pool):
            await _maybe_reflect_on_return("new_user")  # no exception, no send

    @pytest.mark.asyncio
    async def test_skips_when_user_still_active(self):
        """Last message < 8h ago = still active, no greeting."""
        from app.chat import _maybe_reflect_on_return

        recent = datetime.now(timezone.utc) - timedelta(hours=2)
        pool = _FakePool([
            (recent,),
            [("user", "hello"), ("assistant", "hi")],
        ])

        haiku = AsyncMock()

        with patch("app.db.get_pool", return_value=pool), \
             patch(_HAIKU, haiku):
            await _maybe_reflect_on_return("alice")

        haiku.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_user_away_too_long(self):
        """Last message > 72h ago = stale; skip."""
        from app.chat import _maybe_reflect_on_return

        old = datetime.now(timezone.utc) - timedelta(hours=100)
        pool = _FakePool([
            (old,),
            [("user", "hello")],
        ])

        haiku = AsyncMock()

        with patch("app.db.get_pool", return_value=pool), \
             patch(_HAIKU, haiku):
            await _maybe_reflect_on_return("alice")

        haiku.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_triggers_greeting_in_window(self):
        """12h away + messages present + connected -> greeting sent."""
        from app.chat import _maybe_reflect_on_return

        away = datetime.now(timezone.utc) - timedelta(hours=12)
        pool = _FakePool([
            (away,),
            [("user", "we talked about dreams"), ("assistant", "i liked that")],
        ])

        haiku = AsyncMock(return_value="Welcome back, Commander. I kept thinking about dreams.")

        fake_ws = MagicMock()
        fake_ws.is_connected = MagicMock(return_value=True)
        fake_ws.send_proactive = AsyncMock()

        fake_aff = MagicMock()
        fake_aff.get_state = AsyncMock(return_value=_mk_affection())

        with patch("app.db.get_pool", return_value=pool), \
             patch(_HAIKU, haiku), \
             patch("app.reflect_helpers.ws", fake_ws), \
             patch("app.reflect_helpers.affection", fake_aff), \
             patch("app.chat.load_personality", return_value={"user_title": "Commander"}, create=True), \
             patch("app.personality.load_personality", return_value={"user_title": "Commander"}), \
             patch("app.personality.build_character_preamble", return_value="You are Klukai."), \
             patch("asyncio.sleep", new=AsyncMock(return_value=None)):
            await _maybe_reflect_on_return("alice")

        assert haiku.await_count == 1
        assert haiku.await_args.kwargs["route"] == "return_greeting"
        assert haiku.await_args.kwargs["system"] == "You are Klukai."
        assert "we talked about dreams" in haiku.await_args.args[0]
        fake_ws.send_proactive.assert_awaited_once_with(
            "alice", "Welcome back, Commander. I kept thinking about dreams."
        )

    @pytest.mark.asyncio
    async def test_skips_send_when_ws_disconnected(self):
        """LLM produced greeting but user has disconnected by the time it's ready — skip send."""
        from app.chat import _maybe_reflect_on_return

        away = datetime.now(timezone.utc) - timedelta(hours=12)
        pool = _FakePool([
            (away,),
            [("user", "hello")],
        ])

        haiku = AsyncMock(return_value="Welcome back, Commander.")

        fake_ws = MagicMock()
        fake_ws.is_connected = MagicMock(return_value=False)
        fake_ws.send_proactive = AsyncMock()

        fake_aff = MagicMock()
        fake_aff.get_state = AsyncMock(return_value=_mk_affection())

        with patch("app.db.get_pool", return_value=pool), \
             patch(_HAIKU, haiku), \
             patch("app.reflect_helpers.ws", fake_ws), \
             patch("app.reflect_helpers.affection", fake_aff), \
             patch("app.personality.load_personality", return_value={"user_title": "Commander"}), \
             patch("app.personality.build_character_preamble", return_value="You are Klukai."), \
             patch("asyncio.sleep", new=AsyncMock(return_value=None)):
            await _maybe_reflect_on_return("alice")

        fake_ws.send_proactive.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_short_greeting(self):
        """Greetings < 10 chars are discarded (the model probably misfired)."""
        from app.chat import _maybe_reflect_on_return

        away = datetime.now(timezone.utc) - timedelta(hours=12)
        pool = _FakePool([
            (away,),
            [("user", "hello")],
        ])

        haiku = AsyncMock(return_value="ok")

        fake_ws = MagicMock()
        fake_ws.is_connected = MagicMock(return_value=True)
        fake_ws.send_proactive = AsyncMock()

        fake_aff = MagicMock()
        fake_aff.get_state = AsyncMock(return_value=_mk_affection())

        with patch("app.db.get_pool", return_value=pool), \
             patch(_HAIKU, haiku), \
             patch("app.reflect_helpers.ws", fake_ws), \
             patch("app.reflect_helpers.affection", fake_aff), \
             patch("app.personality.load_personality", return_value={"user_title": "Commander"}), \
             patch("app.personality.build_character_preamble", return_value="You are Klukai."), \
             patch("asyncio.sleep", new=AsyncMock(return_value=None)):
            await _maybe_reflect_on_return("alice")

        fake_ws.send_proactive.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_greeting_when_haiku_is_unavailable(self):
        """Haiku capped or failed: she stays quiet; no canned line, no local model."""
        from app.chat import _maybe_reflect_on_return
        from app.extraction_llm import ExtractionUnavailable

        away = datetime.now(timezone.utc) - timedelta(hours=13)
        pool = _FakePool([
            (away,),
            [("user", "hello")],
        ])
        haiku = AsyncMock(side_effect=ExtractionUnavailable("cap"))

        fake_ws = MagicMock()
        fake_ws.is_connected = MagicMock(return_value=True)
        fake_ws.send_proactive = AsyncMock()

        fake_aff = MagicMock()
        fake_aff.get_state = AsyncMock(return_value=_mk_affection())

        with patch("app.db.get_pool", return_value=pool), \
             patch(_HAIKU, haiku), \
             patch("app.reflect_helpers.ws", fake_ws), \
             patch("app.reflect_helpers.affection", fake_aff), \
             patch("app.personality.load_personality", return_value={"user_title": "Commander"}), \
             patch("app.personality.build_character_preamble", return_value="You are Klukai."), \
             patch("asyncio.sleep", new=AsyncMock(return_value=None)):
            await _maybe_reflect_on_return("alice")

        haiku.assert_awaited_once()
        fake_ws.send_proactive.assert_not_called()

    @pytest.mark.asyncio
    async def test_swallows_db_errors(self):
        """DB error must not propagate."""
        from app.chat import _maybe_reflect_on_return

        def broken():
            raise RuntimeError("db down")

        with patch("app.db.get_pool", side_effect=broken):
            await _maybe_reflect_on_return("alice")  # should not raise

    @pytest.mark.asyncio
    async def test_constants_sanity(self):
        """Sanity check: thresholds must be sensibly ordered."""
        from app.chat import REFLECTION_MIN_HOURS_AWAY, REFLECTION_MAX_HOURS_AWAY
        assert REFLECTION_MIN_HOURS_AWAY > 0
        assert REFLECTION_MAX_HOURS_AWAY > REFLECTION_MIN_HOURS_AWAY
