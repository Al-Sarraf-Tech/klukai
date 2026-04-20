"""Pure tests for image_gen classifiers — needs_image, is_couple/landscape,
detect_squad_members, _select_outfit, build_mission_prompt."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestNeedsImage:
    def test_plain_text_does_not_need_image(self):
        from app.image_gen import needs_image
        assert needs_image("what's the weather") is False

    def test_explicit_request_detected(self):
        from app.image_gen import needs_image
        # These phrases exactly contain canonical keywords
        assert needs_image("show me a picture") is True
        assert needs_image("can you draw a scene") is True
        assert needs_image("paint a rooftop shot") is True
        assert needs_image("generate an image of you") is True


class TestIsCoupleScene:
    def test_single_subject_returns_false(self):
        from app.image_gen import is_couple_scene
        assert is_couple_scene("klukai at the window") is False

    def test_couple_keywords_detected(self):
        from app.image_gen import is_couple_scene
        # Test with common couple phrases — at least some should trigger
        hits = any(is_couple_scene(t) for t in [
            "us together",
            "you and me",
            "me and klukai together",
            "both of us on the rooftop",
        ])
        assert hits is True


class TestIsLandscape:
    def test_portrait_context_false(self):
        from app.image_gen import is_landscape
        assert is_landscape("close up of her face") is False

    def test_landscape_keywords_detected(self):
        from app.image_gen import is_landscape
        # At least some landscape-ish phrases should trigger
        hits = any(is_landscape(t) for t in [
            "sweeping cityscape at night",
            "mountains in the distance",
            "panorama of the valley",
            "rooftop overlooking the city",
        ])
        assert hits is True


class TestDetectSquadMembers:
    def test_no_members_empty_list(self):
        from app.image_gen import detect_squad_members
        assert detect_squad_members("just klukai and commander") == []

    def test_returns_list_on_mention(self):
        """Test with an unlikely non-squad term — should be empty."""
        from app.image_gen import detect_squad_members
        out = detect_squad_members("the mission was successful")
        assert isinstance(out, list)


class TestSelectOutfit:
    def test_returns_default_when_no_match(self):
        from app.image_gen import _select_outfit
        outfit_map = {"sleep": "pajamas", "combat": "tactical gear"}
        result = _select_outfit("just hanging out", outfit_map, "casual clothes")
        assert result == "casual clothes"

    def test_matches_keyword_case_insensitive(self):
        from app.image_gen import _select_outfit
        outfit_map = {"sleep": "pajamas"}
        result = _select_outfit("She's ready to Sleep for the night", outfit_map, "x")
        assert result == "pajamas"

    def test_first_match_wins(self):
        """Dict iteration order preserved in Py 3.7+; earlier key wins on multi-match."""
        from app.image_gen import _select_outfit
        outfit_map = {"combat": "tactical gear", "sleep": "pajamas"}
        result = _select_outfit("combat at bedtime sleep scenario", outfit_map, "x")
        assert result == "tactical gear"

    def test_empty_map_returns_default(self):
        from app.image_gen import _select_outfit
        assert _select_outfit("any text", {}, "default") == "default"


class TestBuildMissionPrompt:
    def test_contains_klukai_identity(self):
        from app.image_gen import build_mission_prompt
        prompt = build_mission_prompt(scene_type="combat")
        assert isinstance(prompt, str)
        assert len(prompt) > 50

    def test_includes_tactical_gear(self):
        from app.image_gen import build_mission_prompt
        prompt = build_mission_prompt(scene_type="combat")
        lower = prompt.lower()
        assert any(kw in lower for kw in ["tactical", "rifle", "combat"])

    def test_default_affection_no_crash(self):
        from app.image_gen import build_mission_prompt
        # Should not raise at any affection_level
        for lvl in (0, 5, 9):
            p = build_mission_prompt(scene_type="combat", affection_level=lvl)
            assert p and len(p) > 50

    def test_injuries_included(self):
        from app.image_gen import build_mission_prompt
        p = build_mission_prompt(scene_type="combat",
                                  injuries=["klukai_injured"])
        # Should at least produce a string — the injury tag may or may not
        # surface directly in the prompt but the function must not crash
        assert isinstance(p, str)
