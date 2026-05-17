"""Tests for the crown-jewel block in personality.state_blocks."""

from __future__ import annotations

from app.personality.state_blocks import build_crown_jewel_block


CROWN_TEXT = "Test crown jewel words from Commander to Klukai."
CROWN = {
    "id": "test-id",
    "text": CROWN_TEXT,
    "mood_at_time": "grateful",
    "affection_at_time": 800,
    "created_at": "2026-05-17T00:00:00+00:00",
}


class TestCrownJewelBlock:
    def test_none_returns_empty(self):
        assert build_crown_jewel_block(None, affection_level=9) == ""

    def test_empty_dict_returns_empty(self):
        assert build_crown_jewel_block({}, affection_level=9) == ""

    def test_below_affection_4_returns_empty(self):
        # Affection 0-3: Klukai's guard is still up, no crown jewel reference
        for level in range(4):
            assert build_crown_jewel_block(CROWN, affection_level=level) == "", \
                f"level {level} should suppress crown jewel"

    def test_at_affection_4_includes_block(self):
        out = build_crown_jewel_block(CROWN, affection_level=4)
        assert "TREASURED MEMORY" in out
        assert CROWN_TEXT in out

    def test_at_max_affection_includes_block(self):
        out = build_crown_jewel_block(CROWN, affection_level=9)
        assert "TREASURED MEMORY" in out
        assert CROWN_TEXT in out

    def test_block_includes_usage_guidance(self):
        """The block must steer Klukai to reference naturally, not quote-block."""
        out = build_crown_jewel_block(CROWN, affection_level=9)
        assert "never as a quote-block" in out or "naturally" in out
        assert "vulnerability" in out  # The block hints at when to surface it
        assert "Do not invoke them in every response" in out

    def test_truncates_long_text(self):
        long_text = "X" * 800
        out = build_crown_jewel_block({"text": long_text}, affection_level=9)
        # Truncated to 500 with ellipsis
        assert "X" * 500 not in out
        assert "..." in out

    def test_short_text_preserved(self):
        short_text = "Short."
        out = build_crown_jewel_block({"text": short_text}, affection_level=9)
        assert short_text in out
        assert "..." not in out

    def test_empty_text_returns_empty(self):
        assert build_crown_jewel_block({"text": ""}, affection_level=9) == ""

    def test_whitespace_only_text_returns_empty(self):
        assert build_crown_jewel_block({"text": "   \n  "}, affection_level=9) == ""
