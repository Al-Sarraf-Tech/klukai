"""Pure tests for personality builders — pushes coverage over 50%."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestBuildCharacterPreamble:
    def test_returns_non_empty_string(self):
        from app.personality import build_character_preamble
        p = {"user_title": "Commander",
             "user_description": "the human operator",
             "character": {"name": "Klukai"}}
        out = build_character_preamble(p, affection_level=5)
        assert isinstance(out, str)
        assert len(out) > 10

    def test_includes_user_title(self):
        from app.personality import build_character_preamble
        p = {"user_title": "Captain"}
        out = build_character_preamble(p, affection_level=5)
        assert "Captain" in out

    def test_different_affection_levels_produce_non_empty(self):
        from app.personality import build_character_preamble
        p = {"user_title": "Commander"}
        for lvl in (0, 3, 5, 9):
            out = build_character_preamble(p, affection_level=lvl)
            assert len(out) > 10


class TestBuildCharacterRules:
    def test_returns_string(self):
        from app.personality import build_character_rules
        out = build_character_rules()
        assert isinstance(out, str)
        assert len(out) > 10
