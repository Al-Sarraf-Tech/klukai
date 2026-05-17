"""Tests for personality.rules — character rules, pace block, tool block.

Currently at 62% coverage. Bringing close to 100%.
"""

from __future__ import annotations

from unittest.mock import patch

from app.personality.rules import (
    build_character_rules,
    build_pace_block,
    build_tool_block,
)


class TestCharacterRules:
    def test_contains_absolute_rules_header(self):
        out = build_character_rules()
        assert "ABSOLUTE RULES" in out

    def test_klukai_identity_rule(self):
        out = build_character_rules()
        assert "You ARE Klukai" in out

    def test_forbidden_you_narration(self):
        out = build_character_rules()
        assert "FORBIDDEN" in out
        assert "(You pause)" in out
        # Must explicitly allow (I ...) form
        assert "(I pause)" in out

    def test_no_emoji_rule(self):
        out = build_character_rules()
        assert "Never use emoji" in out

    def test_catchphrase_rule_present(self):
        out = build_character_rules()
        # The actual phrase "all you need" is part of the rules text
        assert "all you need" in out

    def test_no_holograms_rule(self):
        out = build_character_rules()
        assert "holograms" in out.lower() or "holographic" in out.lower()


class TestPaceBlock:
    def test_zero_length_returns_empty(self):
        assert build_pace_block(0) == ""

    def test_very_short_message(self):
        out = build_pace_block(10)
        assert "very short" in out
        assert "1-3 sentences" in out

    def test_brief_message(self):
        out = build_pace_block(30)
        assert "brief" in out
        assert "2-4 sentences" in out

    def test_medium_length_returns_empty(self):
        # 61-300 chars falls through to empty
        assert build_pace_block(150) == ""

    def test_long_message(self):
        out = build_pace_block(500)
        assert "at length" in out

    def test_boundary_15_chars(self):
        # exactly 15 is "very short"
        out = build_pace_block(15)
        assert "very short" in out

    def test_boundary_16_chars(self):
        # 16 falls to "brief"
        out = build_pace_block(16)
        assert "brief" in out

    def test_boundary_60_chars(self):
        # 60 is still "brief"
        out = build_pace_block(60)
        assert "brief" in out

    def test_boundary_61_chars_empty(self):
        # 61-300 is the empty zone
        assert build_pace_block(61) == ""

    def test_boundary_301_chars_long(self):
        # 301 = at length
        out = build_pace_block(301)
        assert "at length" in out


class TestToolBlock:
    def test_disabled_returns_empty(self):
        assert build_tool_block(False) == ""

    def test_enabled_returns_framing(self):
        with patch("app.personality.rules.load_personality") as load:
            load.return_value = {"utility_framing": {"search": "field recon", "fetch": "intel gathering"}}
            out = build_tool_block(True)
        assert "MCP gateway" in out
        assert "field recon" in out
        assert "intel gathering" in out

    def test_enabled_with_empty_framing(self):
        with patch("app.personality.rules.load_personality") as load:
            load.return_value = {}
            out = build_tool_block(True)
        # Should still produce the framing scaffold, just with no items
        assert "MCP gateway" in out
        assert "FRAMING GUIDE:" in out

    def test_framing_table_format(self):
        with patch("app.personality.rules.load_personality") as load:
            load.return_value = {"utility_framing": {"x": "y"}}
            out = build_tool_block(True)
        assert '  - x: frame as "y"' in out
