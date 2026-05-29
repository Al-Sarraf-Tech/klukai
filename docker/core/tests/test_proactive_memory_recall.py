"""Behavioral tests for Living Memory Recall (``_memory_recall_event``).

Klukai surfaces a REAL archive entry as a warm "remember when…" message once
she's bonded. These tests mock the LLM/gate and the memory archive — they never
touch a live LLM or DB. Patterns mirror ``TestDreamEvent`` in
``test_proactive_coverage.py``.
"""

from __future__ import annotations

import contextlib
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.proactive import ProactiveEngine

_NOON = datetime(2026, 5, 17, 14, 0, 0)
_DATETIME_TARGETS = (
    "app.proactive.engine.datetime",
    "app.proactive.events.datetime",
)


@contextlib.contextmanager
def _patch_now(value: datetime = _NOON):
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now.return_value = value
    with contextlib.ExitStack() as stack:
        for target in _DATETIME_TARGETS:
            stack.enter_context(patch(target, mock_dt))
        yield mock_dt


def _recall_engine(affection: int = 5):
    e = ProactiveEngine()
    e._affection_level = affection
    e._memory_recall_delivered_today = False
    e._muted_until = None
    e._last_mood = "tender"
    e._on_message_callback = AsyncMock()
    return e


def _gate():
    gate = AsyncMock()
    gate.__aenter__ = AsyncMock()
    gate.__aexit__ = AsyncMock()
    return gate


_FAKE_MEMORY = {"annotation": "The night on the observation deck, watching the rain."}


class TestMemoryRecallEvent:
    @pytest.mark.asyncio
    async def test_fires_with_memory_at_affection_4(self):
        """Affection >= 4 + a real memory + a working LLM -> delivers LLM text."""
        e = _recall_engine(affection=4)
        gate = _gate()
        with _patch_now(), \
             patch("app.memory_archive.recall_memory",
                   new=AsyncMock(return_value=_FAKE_MEMORY)), \
             patch("app.llm_json.call_llm_text",
                   new=AsyncMock(return_value="Remember the rain on the deck, Commander? I do.")), \
             patch("app.llm_router.get_lm_gate", return_value=gate):
            await e._memory_recall_event()

        e._on_message_callback.assert_awaited_once_with(
            "Remember the rain on the deck, Commander? I do."
        )
        assert e._memory_recall_delivered_today is True
        assert e._proactive_count_today == 1
        assert e._last_proactive_answered is False

    @pytest.mark.asyncio
    async def test_falls_back_to_list_when_recall_empty(self):
        """If recall_memory returns nothing, it pulls from list_memories."""
        e = _recall_engine(affection=6)
        gate = _gate()
        with _patch_now(), \
             patch("app.memory_archive.recall_memory", new=AsyncMock(return_value=None)), \
             patch("app.memory_archive.list_memories",
                   new=AsyncMock(return_value=[_FAKE_MEMORY])), \
             patch("app.proactive.events.random.choice", side_effect=lambda seq: seq[0]), \
             patch("app.llm_json.call_llm_text",
                   new=AsyncMock(return_value="That deck, that rain. I kept it.")), \
             patch("app.llm_router.get_lm_gate", return_value=gate):
            await e._memory_recall_event()

        e._on_message_callback.assert_awaited_once_with("That deck, that rain. I kept it.")
        assert e._memory_recall_delivered_today is True

    @pytest.mark.asyncio
    async def test_does_not_fire_below_affection_4(self):
        e = _recall_engine(affection=3)
        with _patch_now(), \
             patch("app.memory_archive.recall_memory",
                   new=AsyncMock(return_value=_FAKE_MEMORY)) as recall:
            await e._memory_recall_event()
        e._on_message_callback.assert_not_awaited()
        recall.assert_not_awaited()  # gated out before touching the archive
        assert e._memory_recall_delivered_today is False

    @pytest.mark.asyncio
    async def test_does_not_fire_when_archive_empty(self):
        """Empty archive (both recall + list return nothing) -> silent."""
        e = _recall_engine(affection=5)
        with _patch_now(), \
             patch("app.memory_archive.recall_memory", new=AsyncMock(return_value=None)), \
             patch("app.memory_archive.list_memories", new=AsyncMock(return_value=[])):
            await e._memory_recall_event()
        e._on_message_callback.assert_not_awaited()
        assert e._memory_recall_delivered_today is False

    @pytest.mark.asyncio
    async def test_empty_annotation_stays_silent(self):
        """A memory with a blank annotation is not worth reminiscing — silent."""
        e = _recall_engine(affection=5)
        with _patch_now(), \
             patch("app.memory_archive.recall_memory",
                   new=AsyncMock(return_value={"annotation": "   "})):
            await e._memory_recall_event()
        e._on_message_callback.assert_not_awaited()
        assert e._memory_recall_delivered_today is False

    @pytest.mark.asyncio
    async def test_respects_once_per_day_flag(self):
        """Second call within the same day is a no-op."""
        e = _recall_engine(affection=5)
        e._memory_recall_delivered_today = True
        with _patch_now(), \
             patch("app.memory_archive.recall_memory",
                   new=AsyncMock(return_value=_FAKE_MEMORY)) as recall:
            await e._memory_recall_event()
        e._on_message_callback.assert_not_awaited()
        recall.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_muted_skips(self):
        e = _recall_engine(affection=5)
        e._muted_until = datetime(9999, 1, 1)
        with _patch_now(), \
             patch("app.memory_archive.recall_memory",
                   new=AsyncMock(return_value=_FAKE_MEMORY)):
            await e._memory_recall_event()
        e._on_message_callback.assert_not_awaited()
        assert e._memory_recall_delivered_today is False

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_template(self):
        """LLM raises -> a non-empty fallback message is still delivered."""
        e = _recall_engine(affection=7)
        gate = _gate()
        with _patch_now(), \
             patch("app.memory_archive.recall_memory",
                   new=AsyncMock(return_value=_FAKE_MEMORY)), \
             patch("app.llm_json.call_llm_text",
                   new=AsyncMock(side_effect=RuntimeError("LM down"))), \
             patch("app.llm_router.get_lm_gate", return_value=gate):
            await e._memory_recall_event()  # must not raise

        e._on_message_callback.assert_awaited_once()
        delivered = e._on_message_callback.call_args.args[0]
        assert delivered and delivered.strip()  # non-empty fallback
        # Fallback must NOT quote the journal entry verbatim.
        assert "observation deck" not in delivered
        assert e._memory_recall_delivered_today is True

    @pytest.mark.asyncio
    async def test_empty_llm_string_falls_back_to_template(self):
        """LLM returns an empty string -> fallback template is delivered."""
        e = _recall_engine(affection=5)
        gate = _gate()
        with _patch_now(), \
             patch("app.memory_archive.recall_memory",
                   new=AsyncMock(return_value=_FAKE_MEMORY)), \
             patch("app.llm_json.call_llm_text", new=AsyncMock(return_value="   ")), \
             patch("app.llm_router.get_lm_gate", return_value=gate):
            await e._memory_recall_event()

        e._on_message_callback.assert_awaited_once()
        delivered = e._on_message_callback.call_args.args[0]
        assert delivered and delivered.strip()

    @pytest.mark.asyncio
    async def test_archive_exception_returns_silently(self):
        """If the archive lookup itself raises, no message and no flag flip."""
        e = _recall_engine(affection=5)
        with _patch_now(), \
             patch("app.memory_archive.recall_memory",
                   new=AsyncMock(side_effect=RuntimeError("archive down"))):
            await e._memory_recall_event()  # must not raise
        e._on_message_callback.assert_not_awaited()
        assert e._memory_recall_delivered_today is False


class TestMemoryRecallTick:
    @pytest.mark.asyncio
    async def test_tick_skips_on_failed_probability_roll(self):
        """random() above 0.35 -> the tick does not run the event."""
        e = _recall_engine(affection=5)
        with patch("app.proactive.events.random.random", return_value=0.99), \
             patch.object(e, "_memory_recall_event", new=AsyncMock()) as ev:
            await e._memory_recall_tick()
        ev.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tick_runs_event_on_passing_roll(self):
        """random() at/below 0.35 -> the tick runs the event."""
        e = _recall_engine(affection=5)
        with patch("app.proactive.events.random.random", return_value=0.10), \
             patch.object(e, "_memory_recall_event", new=AsyncMock()) as ev:
            await e._memory_recall_tick()
        ev.assert_awaited_once()
