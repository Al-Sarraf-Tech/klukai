"""Send-gate coverage for lore, recall, and dreams.

Dreams are written at night by the cron, inside quiet hours. The gate must
refuse that push before any model call. Lore and recall share the same gate,
including an unanswered earlier line and the daily cap.
"""

from __future__ import annotations

import contextlib
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.proactive import MAX_PROACTIVE_PER_DAY, ProactiveEngine

_NIGHT = datetime(2026, 5, 17, 2, 37, 0)
_NOON = datetime(2026, 5, 17, 14, 0, 0)
_NOW = (
    "app.proactive.engine.now_local",
    "app.proactive.events.now_local",
)


@contextlib.contextmanager
def _patch_now(value: datetime):
    mock_dt = MagicMock(return_value=value)
    with contextlib.ExitStack() as stack:
        for target in _NOW:
            stack.enter_context(patch(target, mock_dt))
        yield


def _engine() -> ProactiveEngine:
    engine = ProactiveEngine()
    engine._affection_level = 8
    engine._on_message_callback = AsyncMock()
    return engine


class TestDreamStaysQuiet:
    @pytest.mark.asyncio
    async def test_night_cron_does_not_page_or_call_the_model(self):
        engine = _engine()
        with (
            _patch_now(_NIGHT),
            patch("app.proactive.events.random.random", return_value=0.0),
            patch("app.background_llm.text", new=AsyncMock()) as model,
        ):
            await engine._dream_event()
        engine._on_message_callback.assert_not_awaited()
        model.assert_not_awaited()
        assert engine._dream_delivered_today is False


class TestLoreSharesTheGate:
    @pytest.mark.asyncio
    async def test_unanswered_line_blocks_lore_before_the_roll(self):
        engine = _engine()
        engine._affection_level = 0
        engine._last_proactive_answered = False
        with (
            _patch_now(_NOON),
            patch("app.proactive.events.random.random", return_value=0.0),
            patch("app.personality.load_personality") as load,
        ):
            await engine._random_event()
        engine._on_message_callback.assert_not_awaited()
        load.assert_not_called()
        assert engine._random_events_today == 0

    @pytest.mark.asyncio
    async def test_shared_cap_blocks_lore(self):
        engine = _engine()
        engine._affection_level = 0
        engine._proactive_count_today = MAX_PROACTIVE_PER_DAY
        with (
            _patch_now(_NOON),
            patch("app.proactive.events.random.random", return_value=0.0),
            patch("app.personality.load_personality") as load,
        ):
            await engine._random_event()
        engine._on_message_callback.assert_not_awaited()
        load.assert_not_called()


class TestRecallSharesTheGate:
    @pytest.mark.asyncio
    async def test_unanswered_line_does_not_open_the_archive(self):
        engine = _engine()
        engine._affection_level = 6
        engine._last_proactive_answered = False
        with (
            _patch_now(_NOON),
            patch(
                "app.memory_archive.recall_memory",
                new=AsyncMock(),
            ) as recall,
        ):
            await engine._memory_recall_event()
        engine._on_message_callback.assert_not_awaited()
        recall.assert_not_awaited()
        assert engine._memory_recall_delivered_today is False

    @pytest.mark.asyncio
    async def test_prompt_does_not_announce_remembering(self):
        engine = _engine()
        engine._affection_level = 6
        captured: dict[str, str] = {}

        async def fake_text(_route, prompt, **_kwargs):
            captured["prompt"] = prompt
            return "The rain on the glass. I kept the window shut."

        with (
            _patch_now(_NOON),
            patch(
                "app.memory_archive.recall_memory",
                new=AsyncMock(
                    return_value={"annotation": "Rain on the observation deck."}
                ),
            ),
            patch("app.background_llm.text", new=fake_text),
        ):
            await engine._memory_recall_event()

        prompt = captured["prompt"].lower()
        assert "do not say 'remember when'" in prompt
        assert "remember when…" not in prompt
        assert "do not say you remember" in prompt
        engine._on_message_callback.assert_awaited_once()
        assert engine._proactive_count_today == 1
