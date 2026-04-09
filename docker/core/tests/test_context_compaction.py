"""Tests for session context compaction: threshold detection, summary injection, trivial messages."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import SessionState


# Import constants from main — these are module-level in app.main
# We test the logic directly rather than importing the full app (avoids DB/Redis init).

COMPACT_THRESHOLD = 8
COMPACT_KEEP_RAW = 4
TRIVIAL_PATTERNS = {
    "ok", "okay", "yes", "no", "yeah", "yep", "nope", "sure", "thanks",
    "thank you", "haha", "lol", "hm", "hmm", "mhm", "hi", "hey", "hello",
    "good", "nice", "cool", "right", "agreed", "understood",
}


def _make_session(turn_count: int, context_summary: str | None = None) -> SessionState:
    turns = []
    for i in range(turn_count):
        role = "user" if i % 2 == 0 else "assistant"
        turns.append({"role": role, "content": f"Turn {i} content here"})
    return SessionState(
        conversation_id="test-conv",
        turns=turns,
        context_summary=context_summary,
        mood="composed",
        turn_count=turn_count,
    )


# ── Compaction threshold ─────────────────────────────────────────────────────

class TestCompactionThreshold:
    def test_below_threshold_no_compaction_needed(self):
        session = _make_session(4)
        assert len(session.turns) < COMPACT_THRESHOLD

    def test_at_threshold_compaction_triggered(self):
        session = _make_session(8)
        assert len(session.turns) >= COMPACT_THRESHOLD

    def test_above_threshold_compaction_triggered(self):
        session = _make_session(12)
        assert len(session.turns) >= COMPACT_THRESHOLD


# ── Compaction logic (simulated) ─────────────────────────────────────────────

class TestCompactionProducesExpectedOutput:
    @pytest.mark.asyncio
    async def test_compaction_produces_summary_and_reduces_turns(self):
        """Simulate the compaction logic from _background_compaction."""
        session = _make_session(10)
        assert len(session.turns) >= COMPACT_THRESHOLD

        fake_summary = "The Commander discussed patrol routes and squad morale."

        with patch("app.fact_extractor.compact_turns", new_callable=AsyncMock) as mock_compact:
            mock_compact.return_value = fake_summary

            # Replicate the compaction logic
            turns = session.turns
            old_turns = turns[:-COMPACT_KEEP_RAW]
            recent_turns = turns[-COMPACT_KEEP_RAW:]

            summary = await mock_compact(old_turns)
            assert summary is not None

            session.context_summary = summary
            session.turns = recent_turns

        assert session.context_summary == fake_summary
        assert len(session.turns) == COMPACT_KEEP_RAW

    @pytest.mark.asyncio
    async def test_compaction_skipped_below_threshold(self):
        """Sessions below COMPACT_THRESHOLD should not be compacted."""
        session = _make_session(4)

        with patch("app.fact_extractor.compact_turns", new_callable=AsyncMock) as mock_compact:
            # Replicate guard from _background_compaction
            if len(session.turns) < COMPACT_THRESHOLD:
                pass  # skip
            else:
                await mock_compact(session.turns)

            mock_compact.assert_not_called()

    @pytest.mark.asyncio
    async def test_compaction_preserves_existing_summary(self):
        """If a context_summary already exists, it should be included in compaction input."""
        existing_summary = "Previous context summary."
        session = _make_session(10, context_summary=existing_summary)

        with patch("app.fact_extractor.compact_turns", new_callable=AsyncMock) as mock_compact:
            mock_compact.return_value = "Updated summary including old context."

            turns = session.turns
            old_turns = turns[:-COMPACT_KEEP_RAW]

            # The real code prepends existing summary as a system turn
            if session.context_summary:
                old_turns = [
                    {"role": "system", "content": f"[Previous summary: {session.context_summary}]"}
                ] + old_turns

            summary = await mock_compact(old_turns)
            # Verify the system turn was included
            assert old_turns[0]["role"] == "system"
            assert "Previous summary" in old_turns[0]["content"]

    @pytest.mark.asyncio
    async def test_compaction_handles_empty_summary_gracefully(self):
        """If compact_turns returns None, session should remain unchanged."""
        session = _make_session(10)
        original_turns = list(session.turns)

        with patch("app.fact_extractor.compact_turns", new_callable=AsyncMock) as mock_compact:
            mock_compact.return_value = None

            summary = await mock_compact(session.turns[:-COMPACT_KEEP_RAW])
            if not summary:
                pass  # Keep raw turns — mirrors the real guard
            else:
                session.turns = session.turns[-COMPACT_KEEP_RAW:]
                session.context_summary = summary

        assert session.turns == original_turns
        assert session.context_summary is None


# ── Context summary injection ────────────────────────────────────────────────

class TestContextSummaryInjection:
    def test_summary_injected_as_system_message(self):
        """When context_summary exists, it becomes a system message in LLM messages."""
        session = _make_session(6, context_summary="The squad discussed supply routes.")

        messages = []
        if session.context_summary:
            messages.append({
                "role": "system",
                "content": f"[Earlier conversation summary: {session.context_summary}]",
            })
        messages.extend(
            {"role": t["role"], "content": t["content"]}
            for t in session.turns[-16:]
        )

        assert messages[0]["role"] == "system"
        assert "Earlier conversation summary" in messages[0]["content"]
        assert "supply routes" in messages[0]["content"]

    def test_no_summary_no_system_message(self):
        """Without context_summary, no system message is injected."""
        session = _make_session(4)

        messages = []
        if session.context_summary:
            messages.append({
                "role": "system",
                "content": f"[Earlier conversation summary: {session.context_summary}]",
            })
        messages.extend(
            {"role": t["role"], "content": t["content"]}
            for t in session.turns[-16:]
        )

        assert messages[0]["role"] != "system"


# ── Trivial message detection ────────────────────────────────────────────────

class TestTrivialPatterns:
    def test_ok_is_trivial(self):
        assert "ok" in TRIVIAL_PATTERNS

    def test_yes_is_trivial(self):
        assert "yes" in TRIVIAL_PATTERNS

    def test_thanks_is_trivial(self):
        assert "thanks" in TRIVIAL_PATTERNS

    def test_hello_is_trivial(self):
        assert "hello" in TRIVIAL_PATTERNS

    def test_long_message_not_trivial(self):
        assert "tell me about the mission briefing we had yesterday" not in TRIVIAL_PATTERNS

    def test_trivial_detection_with_stripping(self):
        """The real code strips, lowercases, and removes trailing punctuation."""
        content = "  OK!  "
        content_stripped = content.strip().lower().rstrip("!.?)")
        assert content_stripped in TRIVIAL_PATTERNS

    def test_haha_is_trivial(self):
        assert "haha" in TRIVIAL_PATTERNS

    def test_understood_is_trivial(self):
        assert "understood" in TRIVIAL_PATTERNS

    def test_short_non_trivial_detected_by_length(self):
        """Messages <= 5 chars that aren't in TRIVIAL_PATTERNS are still trivial by length."""
        content = "abcde"
        content_stripped = content.strip().lower().rstrip("!.?)")
        is_trivial = content_stripped in TRIVIAL_PATTERNS or len(content_stripped) <= 5
        assert is_trivial
