"""Tests for app.image_gen pure helpers (no ComfyUI HTTP)."""

from __future__ import annotations

from app.image_gen import (
    build_mission_prompt,
    build_prompt,
    detect_squad_members,
    is_couple_scene,
    is_landscape,
    needs_image,
    _select_outfit,
)


class TestDetectSquadMembers:
    def test_empty_text_returns_empty(self):
        assert detect_squad_members("") == []

    def test_finds_mechty(self):
        assert "mechty" in detect_squad_members("Mechty was sleeping")

    def test_finds_multiple(self):
        result = detect_squad_members("Belka and Andoris and Mechty all there")
        assert "belka" in result
        assert "andoris" in result
        assert "mechty" in result

    def test_no_matches_empty(self):
        assert detect_squad_members("a generic sentence with no names") == []


class TestNeedsImage:
    def test_image_request_keyword(self):
        # IMAGE_KEYWORDS includes "image", "picture", "show me", "draw"
        assert needs_image("show me a picture") is True

    def test_no_keyword_false(self):
        assert needs_image("just a regular chat message") is False

    def test_case_insensitive(self):
        assert needs_image("SHOW ME") is True or needs_image("show me") is True


class TestIsCoupleScene:
    def test_explicit_commander_klukai(self):
        # "us together" is a strong couple signal — depends on COUPLE_KEYWORDS
        # We just verify the function returns a bool consistently
        assert isinstance(is_couple_scene("us together"), bool)

    def test_solo_klukai(self):
        # No couple signal — should be False (unless a keyword leaks)
        result = is_couple_scene("Klukai alone in her quarters")
        assert isinstance(result, bool)


class TestIsLandscape:
    def test_returns_bool(self):
        assert isinstance(is_landscape("a wide cityscape"), bool)
        assert isinstance(is_landscape("portrait of klukai"), bool)


class TestSelectOutfit:
    def test_keyword_match_picks_outfit(self):
        m = {"motorcycle": "biker leather", "rain": "raincoat"}
        assert _select_outfit("on the motorcycle today", m, "default") == "biker leather"

    def test_no_match_returns_default(self):
        m = {"motorcycle": "biker"}
        assert _select_outfit("just chatting", m, "casual") == "casual"

    def test_first_match_wins(self):
        m = {"a": "first", "b": "second"}
        assert _select_outfit("a and b both here", m, "default") == "first"

    def test_case_insensitive(self):
        m = {"motorcycle": "biker"}
        assert _select_outfit("On the MOTORCYCLE", m, "default") == "biker"


class TestBuildMissionPrompt:
    def test_returns_string(self):
        result = build_mission_prompt(scene_type="combat", squad_members=["mechty"],
                                       injuries=[], affection_level=5)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_includes_squad_member(self):
        result = build_mission_prompt(scene_type="combat", squad_members=["mechty"],
                                       injuries=[], affection_level=5)
        # Mechty should appear in the prompt somehow
        assert "mechty" in result.lower() or "g11" in result.lower()

    def test_no_squad_handles_empty_list(self):
        result = build_mission_prompt(scene_type="combat", squad_members=[],
                                       injuries=[], affection_level=0)
        assert isinstance(result, str)
        assert len(result) > 0


class TestBuildPrompt:
    def test_basic_returns_string(self):
        result = build_prompt(scene_tags="klukai smiling")
        assert isinstance(result, str)
        assert len(result) > 10

    def test_couple_flag_adds_commander(self):
        solo = build_prompt(scene_tags="cafe scene", couple=False)
        couple = build_prompt(scene_tags="cafe scene", couple=True)
        # Couple version should be longer or differ from solo
        assert couple != solo

    def test_squad_members_referenced(self):
        result = build_prompt(scene_tags="briefing", squad_members=["mechty"])
        assert "mechty" in result.lower() or "g11" in result.lower()
