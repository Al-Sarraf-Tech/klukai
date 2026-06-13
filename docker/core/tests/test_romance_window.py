"""Tests for the evening romance window in ProactiveEngine."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.proactive import ProactiveEngine, ROMANCE_MESSAGES


def _make_engine(
    affection_level: int = 5,
    user_messaged: bool = True,
    mood: str = "composed",
    romance_delivered: bool = False,
    muted: bool = False,
    last_proactive_answered: bool = True,
) -> ProactiveEngine:
    """Build a ProactiveEngine with controlled state for testing."""
    engine = ProactiveEngine()
    engine._affection_level = affection_level
    engine._user_messaged_today = user_messaged
    engine._last_mood = mood
    engine._romance_delivered_today = romance_delivered
    engine._last_proactive_answered = last_proactive_answered
    if muted:
        engine._muted_until = datetime(9999, 12, 31)
    else:
        engine._muted_until = None
    return engine


# ── Affection gate ───────────────────────────────────────────────────────────

class TestRomanceAffectionGate:
    @pytest.mark.asyncio
    async def test_romance_does_not_fire_at_affection_0(self):
        engine = _make_engine(affection_level=0)
        callback = AsyncMock()
        engine._on_message_callback = callback

        # Patch sleep to not actually wait
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await engine._romance_window()

        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_romance_does_not_fire_at_affection_1(self):
        engine = _make_engine(affection_level=1)
        callback = AsyncMock()
        engine._on_message_callback = callback

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await engine._romance_window()

        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_romance_does_not_fire_at_affection_2(self):
        engine = _make_engine(affection_level=2)
        callback = AsyncMock()
        engine._on_message_callback = callback

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await engine._romance_window()

        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_romance_fires_at_affection_3(self):
        engine = _make_engine(affection_level=3)
        callback = AsyncMock()
        engine._on_message_callback = callback

        with patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("app.proactive.events.publish_event", new_callable=AsyncMock):
            await engine._romance_window()

        callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_romance_fires_at_affection_9(self):
        engine = _make_engine(affection_level=9)
        callback = AsyncMock()
        engine._on_message_callback = callback

        # At level 9 (>= 5), the LLM path is used. Mock it.
        with patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("app.proactive.events.publish_event", new_callable=AsyncMock), \
             patch("app.fact_extractor.generate_romance_message", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = "The stars remind me of you, Commander."
            await engine._romance_window()

        callback.assert_called_once()


# ── Template vs. LLM routing ────────────────────────────────────────────────

class TestRomanceMessageSource:
    @pytest.mark.asyncio
    async def test_levels_3_4_use_templates(self):
        """Affection 3-4 should use ROMANCE_MESSAGES templates, not LLM."""
        engine = _make_engine(affection_level=4)
        callback = AsyncMock()
        engine._on_message_callback = callback

        with patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("app.proactive.events.publish_event", new_callable=AsyncMock):
            await engine._romance_window()

        # The callback arg should be a string from the template pool
        delivered_msg = callback.call_args[0][0]
        all_templates = []
        for msgs in ROMANCE_MESSAGES.values():
            all_templates.extend(msgs)
        assert delivered_msg in all_templates

    @pytest.mark.asyncio
    async def test_level_5_uses_llm(self):
        """Affection >= 5 should call generate_romance_message."""
        engine = _make_engine(affection_level=5)
        callback = AsyncMock()
        engine._on_message_callback = callback
        engine._session_getter = None  # No session context

        with patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("app.proactive.events.publish_event", new_callable=AsyncMock), \
             patch("app.fact_extractor.generate_romance_message", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = "LLM-generated romance message."
            await engine._romance_window()

        mock_llm.assert_called_once()
        delivered_msg = callback.call_args[0][0]
        assert delivered_msg == "LLM-generated romance message."

    @pytest.mark.asyncio
    async def test_level_7_uses_llm(self):
        engine = _make_engine(affection_level=7)
        callback = AsyncMock()
        engine._on_message_callback = callback

        with patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("app.proactive.events.publish_event", new_callable=AsyncMock), \
             patch("app.fact_extractor.generate_romance_message", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = "Evening warmth."
            await engine._romance_window()

        mock_llm.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_template(self):
        """If generate_romance_message raises, should fall back to template."""
        engine = _make_engine(affection_level=6)
        callback = AsyncMock()
        engine._on_message_callback = callback

        with patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("app.proactive.events.publish_event", new_callable=AsyncMock), \
             patch("app.fact_extractor.generate_romance_message", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = Exception("LLM down")
            await engine._romance_window()

        # Should still deliver (via template fallback)
        callback.assert_called_once()


# ── User activity gate ───────────────────────────────────────────────────────

class TestRomanceUserActivityGate:
    @pytest.mark.asyncio
    async def test_no_romance_if_user_hasnt_messaged_today(self):
        engine = _make_engine(affection_level=5, user_messaged=False)
        callback = AsyncMock()
        engine._on_message_callback = callback

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await engine._romance_window()

        callback.assert_not_called()


# ── Comfort when stressed ────────────────────────────────────────────────────

class TestRomanceComfortWhenStressed:
    @pytest.mark.asyncio
    async def test_stressed_mood_delivers_comfort(self):
        """When mood is in the negative set, comfort is delivered instead of romance."""
        engine = _make_engine(affection_level=5, mood="irritated")
        callback = AsyncMock()
        engine._on_message_callback = callback

        with patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("app.proactive.events.publish_event", new_callable=AsyncMock):
            await engine._romance_window()

        callback.assert_called_once()
        delivered_msg = callback.call_args[0][0]
        # Comfort messages mention "difficult day", "weighing on you", or "hard"
        comfort_indicators = ["difficult", "weighing", "hard", "carry"]
        assert any(ind in delivered_msg.lower() for ind in comfort_indicators)

    @pytest.mark.asyncio
    async def test_melancholic_mood_delivers_comfort(self):
        engine = _make_engine(affection_level=7, mood="melancholic")
        callback = AsyncMock()
        engine._on_message_callback = callback

        with patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("app.proactive.events.publish_event", new_callable=AsyncMock):
            await engine._romance_window()

        callback.assert_called_once()
        # Should not call LLM for romance
        delivered_msg = callback.call_args[0][0]
        assert isinstance(delivered_msg, str) and len(delivered_msg) > 0


# ── One-shot per night ───────────────────────────────────────────────────────

class TestRomanceOneShot:
    @pytest.mark.asyncio
    async def test_doesnt_fire_twice_in_one_night(self):
        engine = _make_engine(affection_level=5, romance_delivered=True)
        callback = AsyncMock()
        engine._on_message_callback = callback

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await engine._romance_window()

        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_sets_delivered_flag_after_firing(self):
        engine = _make_engine(affection_level=3)
        callback = AsyncMock()
        engine._on_message_callback = callback

        assert engine._romance_delivered_today is False

        with patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("app.proactive.events.publish_event", new_callable=AsyncMock):
            await engine._romance_window()

        assert engine._romance_delivered_today is True

    @pytest.mark.asyncio
    async def test_daily_reset_clears_flag(self):
        engine = _make_engine(affection_level=5, romance_delivered=True)
        await engine._reset_daily()
        assert engine._romance_delivered_today is False
