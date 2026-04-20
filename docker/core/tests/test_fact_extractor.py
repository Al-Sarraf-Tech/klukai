"""Tests for fact_extractor — validation, mood/interaction sanitization, summaries."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ═══════════════════════════════════════════════════════════════════════════
# extract_facts — LLM output validation + defaults
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractFacts:
    @pytest.mark.asyncio
    async def test_returns_defaults_when_llm_returns_none(self):
        from app.fact_extractor import extract_facts, _DEFAULT_RESULT
        from app import llm_router

        class _FakeGate:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return None

        with patch("app.llm_router.get_lm_gate", return_value=_FakeGate(), create=True), \
             patch("app.fact_extractor.call_llm", new=AsyncMock(return_value=None)):
            out = await extract_facts("hi", "hello")

        assert out == dict(_DEFAULT_RESULT)

    @pytest.mark.asyncio
    async def test_invalid_mood_defaults_to_composed(self):
        from app.fact_extractor import extract_facts

        class _FakeGate:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return None

        bad_result = {
            "mood": "rampaging-dragon",  # not in VALID_MOODS
            "facts": [],
            "topics": [],
            "should_remember": False,
            "interaction": {"type": "neutral", "intensity": 5},
        }

        with patch("app.llm_router.get_lm_gate", return_value=_FakeGate()), \
             patch("app.fact_extractor.call_llm", new=AsyncMock(return_value=bad_result)):
            out = await extract_facts("hi", "hello")

        assert out["mood"] == "composed"

    @pytest.mark.asyncio
    async def test_valid_mood_preserved(self):
        from app.fact_extractor import extract_facts, VALID_MOODS

        class _FakeGate:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return None

        # Pick a known-good mood
        good_mood = next(iter(VALID_MOODS))
        result = {
            "mood": good_mood,
            "facts": [],
            "topics": [],
            "should_remember": False,
            "interaction": {"type": "neutral", "intensity": 5},
        }

        with patch("app.llm_router.get_lm_gate", return_value=_FakeGate()), \
             patch("app.fact_extractor.call_llm", new=AsyncMock(return_value=result)):
            out = await extract_facts("x", "y")

        assert out["mood"] == good_mood

    @pytest.mark.asyncio
    async def test_missing_interaction_gets_default(self):
        from app.fact_extractor import extract_facts

        class _FakeGate:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return None

        result = {
            "mood": "composed",
            "facts": [],
            "topics": [],
            "should_remember": False,
            # interaction missing
        }

        with patch("app.llm_router.get_lm_gate", return_value=_FakeGate()), \
             patch("app.fact_extractor.call_llm", new=AsyncMock(return_value=result)):
            out = await extract_facts("x", "y")

        assert out["interaction"]["type"] == "neutral"
        assert out["interaction"]["intensity"] == 5

    @pytest.mark.asyncio
    async def test_malformed_interaction_replaced(self):
        """interaction as a string instead of dict should be replaced."""
        from app.fact_extractor import extract_facts

        class _FakeGate:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return None

        result = {
            "mood": "composed",
            "interaction": "flirty",   # should be dict
        }

        with patch("app.llm_router.get_lm_gate", return_value=_FakeGate()), \
             patch("app.fact_extractor.call_llm", new=AsyncMock(return_value=result)):
            out = await extract_facts("x", "y")

        assert isinstance(out["interaction"], dict)
        assert out["interaction"]["type"] == "neutral"

    @pytest.mark.asyncio
    async def test_image_generated_adds_curation(self):
        """When image_generated=True and result has memory_curation, it's preserved."""
        from app.fact_extractor import extract_facts

        class _FakeGate:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return None

        result = {
            "mood": "composed",
            "facts": [],
            "topics": [],
            "should_remember": False,
            "interaction": {"type": "neutral", "intensity": 5},
            "memory_curation": {"kept": True, "annotation": "nice one"},
        }

        with patch("app.llm_router.get_lm_gate", return_value=_FakeGate()), \
             patch("app.fact_extractor.call_llm", new=AsyncMock(return_value=result)), \
             patch("app.memory_archive.available_categories", return_value=["portrait"]):
            out = await extract_facts("x", "y", image_generated=True)

        assert "memory_curation" in out
        assert out["memory_curation"]["kept"] is True


# ═══════════════════════════════════════════════════════════════════════════
# create_episode_summary — length gating + LLM call
# ═══════════════════════════════════════════════════════════════════════════


class TestCreateEpisodeSummary:
    @pytest.mark.asyncio
    async def test_short_turns_returns_none(self):
        from app.fact_extractor import create_episode_summary
        turns = [{"role": "user", "content": "hi"}]
        assert await create_episode_summary(turns) is None

    @pytest.mark.asyncio
    async def test_exactly_two_turns_returns_none(self):
        from app.fact_extractor import create_episode_summary
        turns = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        assert await create_episode_summary(turns) is None

    @pytest.mark.asyncio
    async def test_enough_turns_calls_llm(self):
        from app.fact_extractor import create_episode_summary

        class _FakeGate:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return None

        turns = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]

        with patch("app.llm_router.get_lm_gate", return_value=_FakeGate()), \
             patch("app.fact_extractor.call_llm_text",
                   new=AsyncMock(return_value="Klukai's little journal.")):
            result = await create_episode_summary(turns)

        assert result == "Klukai's little journal."


# ═══════════════════════════════════════════════════════════════════════════
# compact_turns — window reduction utility
# ═══════════════════════════════════════════════════════════════════════════


class TestCompactTurns:
    @pytest.mark.asyncio
    async def test_empty_turns_returns_none(self):
        from app.fact_extractor import compact_turns
        assert await compact_turns([]) is None

    @pytest.mark.asyncio
    async def test_calls_llm_text_for_compaction(self):
        from app.fact_extractor import compact_turns

        class _FakeGate:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return None

        turns = [{"role": "user", "content": "long"}] * 5
        with patch("app.llm_router.get_lm_gate", return_value=_FakeGate()), \
             patch("app.fact_extractor.call_llm_text",
                   new=AsyncMock(return_value="compact summary")):
            result = await compact_turns(turns)

        assert result == "compact summary"
