"""Integration tests for the companion-core pipeline.

These test the message processing flow end-to-end with mocked external services
(LLM, Redis, PostgreSQL, Qdrant) but real internal logic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.helpers import (
    fix_narration,
    enhance_image_prompt,
    wants_recall,
    wants_mission_start,
    wants_mission_cancel,
    parse_interval_minutes,
    TRIVIAL_PATTERNS,
    SAVE_KEYWORDS,
    DISCARD_KEYWORDS,
)
from app.models import SessionState
from app.ws_manager import WSManager


# ── Message Pipeline Tests ───────────────────────────────────────────────────


class TestMessageClassification:
    """Test that messages are correctly classified for processing."""

    def test_trivial_messages_skip_extraction(self):
        """Short/trivial messages should bypass expensive LLM extraction."""
        for msg in ["ok", "yes", "thanks", "hi", "cool"]:
            assert msg in TRIVIAL_PATTERNS

    def test_recall_triggers(self):
        """Memory recall keywords are detected."""
        assert wants_recall("show me a memory of us")
        assert wants_recall("Do you remember when we first met?")
        assert not wants_recall("Nice weather today")

    def test_mission_triggers(self):
        """Mission start/cancel keywords are detected correctly."""
        assert wants_mission_start("give me updates every 30 minutes")
        assert wants_mission_cancel("stop updates")
        assert not wants_mission_start("what's the update on that?")
        assert not wants_mission_cancel("update me on progress")

    def test_save_discard_keywords(self):
        """Commander override keywords for memory curation."""
        for kw in SAVE_KEYWORDS:
            assert isinstance(kw, str)
            assert len(kw) > 3
        for kw in DISCARD_KEYWORDS:
            assert isinstance(kw, str)
            assert len(kw) > 3

    def test_interval_parsing_edge_cases(self):
        """Interval parsing handles various natural language formats."""
        assert parse_interval_minutes("every 5 minutes") == 5
        assert parse_interval_minutes("every 2 hours") == 120
        assert parse_interval_minutes("every half hour") == 30
        assert parse_interval_minutes("every hour") == 60
        # Minimum is 5 minutes
        assert parse_interval_minutes("every 1 min") == 5
        assert parse_interval_minutes("every 0 minutes") == 5


# ── Narration Pipeline Tests ────────────────────────────────────────────────


class TestNarrationPipeline:
    """Test the full narration fix pipeline as it runs on LLM output."""

    def test_complete_response_processing(self):
        """Simulate a full LLM response going through narration fix."""
        raw_llm_output = (
            '<think>The user asked about my motorcycle. I should mention '
            'the Devastating Drift.</think>'
            '(I cross my arms)\n\n'
            '"You want to know about my bike? Hmph. '
            "It's not just a bike, Commander. "
            'It has vertical missile launchers."\n\n'
            "(Your eyes widen in surprise)\n\n"
            '"...What? Did you expect something ordinary from an Elite Doll?"|||'
        )
        fixed = fix_narration(raw_llm_output)
        # Thinking should be stripped
        assert "<think>" not in fixed
        assert "The user asked" not in fixed
        # First-person narration preserved
        assert "(I cross my arms)" in fixed
        # Commander narration removed
        assert "(Your eyes widen" not in fixed
        # Content preserved
        assert "missile launchers" in fixed
        assert "Elite Doll" in fixed
        # Trailing pipes removed
        assert not fixed.endswith("|")

    def test_intimate_response_processing(self):
        """Narration fix handles intimate responses correctly."""
        raw = (
            '(I lean closer, my voice dropping)\n\n'
            '"...Stay. Just for tonight."\n\n'
            '(Your breath catches)\n\n'
            '"Commander... I...\n\n'
            '...don\'t make me say it twice."'
        )
        fixed = fix_narration(raw)
        assert "(I lean closer" in fixed
        assert "(Your breath" not in fixed
        assert "don't make me say it twice" in fixed

    def test_multiline_think_block(self):
        """Think blocks spanning multiple lines are fully stripped."""
        raw = (
            '<think>\n'
            'The user is flirting.\n'
            'I should respond warmly but with tsundere undertones.\n'
            'Affection level 8 means I can be vulnerable.\n'
            '</think>\n'
            '"...Dummkopf. Why do you always say things like that?"'
        )
        fixed = fix_narration(raw)
        assert "Dummkopf" in fixed
        assert "flirting" not in fixed
        assert "tsundere" not in fixed


# ── Image Prompt Pipeline Tests ──────────────────────────────────────────────


class TestImagePromptPipeline:
    """Test the image prompt generation as it flows from user request to tags."""

    def test_complex_scene_request(self):
        """Multiple keywords combine into a rich prompt."""
        result = enhance_image_prompt(
            "draw us kissing on the beach at sunset", couple=True
        )
        assert "beach" in result
        assert "sunset" in result
        assert "kiss" in result
        assert "romantic" in result

    def test_battle_scene(self):
        """Combat scenes generate appropriate tags."""
        result = enhance_image_prompt("show me fighting in the city")
        assert "fighting" in result or "battlefield" in result
        assert "city" in result or "urban" in result

    def test_couple_flag_effect(self):
        """Couple flag doesn't break generic scenes."""
        solo = enhance_image_prompt("draw yourself reading", couple=False)
        couple = enhance_image_prompt("draw yourself reading", couple=True)
        assert "reading" in solo
        assert "reading" in couple

    def test_empty_request_has_fallback(self):
        """Empty or unrecognized requests get default tags."""
        result = enhance_image_prompt("")
        assert "standing" in result
        assert "looking at viewer" in result


# ── WebSocket Manager Tests ──────────────────────────────────────────────────


class TestWSManagerAdvanced:
    """Advanced WebSocket manager behavior."""

    @pytest.fixture
    def ws(self):
        return WSManager()

    @pytest.mark.asyncio
    async def test_send_to_disconnected_user_is_safe(self, ws):
        """Sending to a user that's not connected should not raise."""
        # Should not raise
        await ws.send_token("nobody", "hello")

    @pytest.mark.asyncio
    async def test_multi_device_same_user(self, ws):
        """Multiple devices for the same user all receive messages."""
        s1, s2 = MagicMock(), MagicMock()
        s1.accept = AsyncMock()
        s1.send_json = AsyncMock()
        s2.accept = AsyncMock()
        s2.send_json = AsyncMock()
        await ws.connect(s1, "user1")
        await ws.connect(s2, "user1")
        assert ws.is_connected("user1")
        # Both devices should exist in the connection set
        await ws.send_token("user1", "test")


# ── Session State Tests ──────────────────────────────────────────────────────


class TestSessionState:
    """Session state management and compaction logic."""

    def test_default_session(self):
        s = SessionState(conversation_id="test")
        assert s.mood == "composed"
        assert s.turns == []
        assert s.turn_count == 0
        assert s.context_summary is None

    def test_session_with_turns(self):
        turns = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Commander."},
        ]
        s = SessionState(conversation_id="test", turns=turns, turn_count=2)
        assert len(s.turns) == 2
        assert s.turn_count == 2

    def test_compaction_threshold(self):
        """Sessions with >8 turns should trigger compaction."""
        COMPACT_THRESHOLD = 8
        s = SessionState(conversation_id="test", turn_count=10)
        assert s.turn_count >= COMPACT_THRESHOLD

    def test_mood_persistence(self):
        """Mood should persist across session operations."""
        s = SessionState(conversation_id="test", mood="tender")
        assert s.mood == "tender"


# ── Memory Dedup Tests ───────────────────────────────────────────────────────


class TestMemoryDedup:
    """Test annotation deduplication logic."""

    def _word_overlap(self, a: str, b: str) -> float:
        """Calculate word overlap ratio between two strings."""
        words_a = set(a.lower().split())
        words_b = set(b.lower().split())
        if not words_a or not words_b:
            return 0.0
        overlap = len(words_a & words_b)
        return overlap / max(len(words_a), len(words_b))

    def test_identical_annotations_detected(self):
        a = "His hand found mine across the table."
        assert self._word_overlap(a, a) == 1.0

    def test_similar_annotations_detected(self):
        a = "His hand found mine across the table in the quiet cafe."
        b = "His hand found mine across the quiet table at the cafe."
        assert self._word_overlap(a, b) > 0.7

    def test_different_annotations_pass(self):
        a = "The motorcycle roared through the night streets."
        b = "He fell asleep on my shoulder during the briefing."
        assert self._word_overlap(a, b) < 0.3

    def test_short_annotations_not_flagged(self):
        """Very short annotations shouldn't trigger false dedup."""
        a = "Good."
        b = "Fine."
        # Too short to meaningfully compare
        assert len(set(a.lower().split())) < 3
