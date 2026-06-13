"""Tests for fact_extractor — validation, mood/interaction sanitization, summaries."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ═══════════════════════════════════════════════════════════════════════════
# extract_facts — LLM output validation + defaults
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractFacts:
    @pytest.mark.asyncio
    async def test_returns_defaults_when_llm_returns_none(self):
        from app.fact_extractor import extract_facts, _DEFAULT_RESULT

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
# extract_promises — commitment detection + confidence filtering
# ═══════════════════════════════════════════════════════════════════════════


class _FakeGate:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return None


class TestExtractPromises:
    @pytest.mark.asyncio
    async def test_none_result_returns_empty(self):
        from app.fact_extractor import extract_promises

        with patch("app.llm_router.get_lm_gate", return_value=_FakeGate(), create=True), \
             patch("app.fact_extractor.call_llm", new=AsyncMock(return_value=None)):
            out = await extract_promises("I'll fix it tomorrow", affection_level=5)
        assert out == {"promises": []}

    @pytest.mark.asyncio
    async def test_keeps_high_confidence_promises(self):
        from app.fact_extractor import extract_promises

        result = {"promises": [
            {"action": "fix the door", "target": "the door",
             "deadline_hint": "tomorrow", "confidence": 0.9},
        ]}
        with patch("app.llm_router.get_lm_gate", return_value=_FakeGate()), \
             patch("app.fact_extractor.call_llm", new=AsyncMock(return_value=result)):
            out = await extract_promises("I'll fix the door tomorrow", affection_level=3)

        assert len(out["promises"]) == 1
        p = out["promises"][0]
        assert p["action"] == "fix the door"
        assert p["deadline_hint"] == "tomorrow"
        assert p["confidence"] == 0.9

    @pytest.mark.asyncio
    async def test_drops_low_confidence(self):
        """confidence < 0.7 must be filtered out."""
        from app.fact_extractor import extract_promises

        result = {"promises": [
            {"action": "maybe call someday", "confidence": 0.4},
            {"action": "submit the report", "confidence": 0.7},  # boundary kept
            {"action": "finish painting", "confidence": 0.95},
        ]}
        with patch("app.llm_router.get_lm_gate", return_value=_FakeGate()), \
             patch("app.fact_extractor.call_llm", new=AsyncMock(return_value=result)):
            out = await extract_promises("...", affection_level=5)

        actions = {p["action"] for p in out["promises"]}
        assert actions == {"submit the report", "finish painting"}

    @pytest.mark.asyncio
    async def test_skips_malformed_items(self):
        """Non-dict items, blank actions, and non-numeric confidence are dropped."""
        from app.fact_extractor import extract_promises

        result = {"promises": [
            "not a dict",
            {"action": "   ", "confidence": 0.9},          # blank action
            {"target": "x", "confidence": 0.9},            # no action key
            {"action": "do it", "confidence": "high"},     # bad confidence
            {"action": "real one", "confidence": 0.8},     # the only keeper
        ]}
        with patch("app.llm_router.get_lm_gate", return_value=_FakeGate()), \
             patch("app.fact_extractor.call_llm", new=AsyncMock(return_value=result)):
            out = await extract_promises("...", affection_level=5)

        assert [p["action"] for p in out["promises"]] == ["real one"]

    @pytest.mark.asyncio
    async def test_promises_not_a_list_returns_empty(self):
        from app.fact_extractor import extract_promises

        with patch("app.llm_router.get_lm_gate", return_value=_FakeGate()), \
             patch("app.fact_extractor.call_llm",
                   new=AsyncMock(return_value={"promises": "nope"})):
            out = await extract_promises("...", affection_level=5)
        assert out == {"promises": []}

    @pytest.mark.asyncio
    async def test_non_dict_result_returns_empty(self):
        from app.fact_extractor import extract_promises

        with patch("app.llm_router.get_lm_gate", return_value=_FakeGate()), \
             patch("app.fact_extractor.call_llm",
                   new=AsyncMock(return_value=["unexpected"])):
            out = await extract_promises("...", affection_level=5)
        assert out == {"promises": []}


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
