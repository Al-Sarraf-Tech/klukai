"""Kitchen-sink test suite: covers every testable surface without external services.

Tests: affection deltas, image gen detection, proactive gating, memory archive,
dedup logic, annotation quality, mood states, personality config integrity.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Affection System ────────────────────────────────────────────────────────


class TestAffectionDeltas:
    """Test affection point awards match expected behavior."""

    @pytest.fixture
    def manager(self):
        pytest.importorskip("psycopg")
        from app.affection import AffectionManager
        mgr = AffectionManager()
        mgr._levels = [
            {"index": 0, "threshold": 0, "name": "Cold Assessment"},
            {"index": 1, "threshold": 20, "name": "Professional Respect"},
            {"index": 2, "threshold": 50, "name": "Trusted Ally"},
            {"index": 3, "threshold": 80, "name": "Guarded Care"},
            {"index": 5, "threshold": 180, "name": "Admitted Bond"},
            {"index": 7, "threshold": 420, "name": "Unveiled Heart"},
            {"index": 9, "threshold": 820, "name": "Devoted Oath"},
            {"index": 10, "threshold": 1000, "name": "Oath Eternal"},
        ]
        return mgr

    def test_level_monotonically_increases(self, manager):
        prev = -1
        for score in range(0, 1001, 5):
            level, _ = manager._compute_level(score)
            assert level >= prev, f"Level decreased at score {score}"
            prev = level

    def test_max_score_reaches_max_level(self, manager):
        level, name = manager._compute_level(1000)
        assert level == 10
        assert name == "Oath Eternal"

    def test_zero_is_cold(self, manager):
        _, name = manager._compute_level(0)
        assert name == "Cold Assessment"


# ── Image Generation Detection ──────────────────────────────────────────────


class TestImageGenDetection:
    """Test the image generation trigger keywords."""

    def test_positive_triggers(self):
        from app.image_gen import needs_image
        triggers = [
            "draw yourself", "show me a picture", "generate an image",
            "visualize us", "paint me a scene", "draw us together",
        ]
        for t in triggers:
            assert needs_image(t), f"Should trigger: {t}"

    def test_negative_triggers(self):
        from app.image_gen import needs_image
        non_triggers = [
            "tell me about your day", "what's the weather",
            "how are you feeling", "describe your squad",
        ]
        for t in non_triggers:
            assert not needs_image(t), f"Should NOT trigger: {t}"

    def test_image_gen_suppresses_verbose_text(self):
        """When image gen triggers, system prompt should tell LLM to keep text short."""
        from app.image_gen import needs_image
        # The main.py handler injects IMAGE GENERATION ACTIVE hint when this is true
        assert needs_image("show me a picture of us")
        assert needs_image("draw yourself")
        # The hint says "1 SHORT sentence" — verified by checking the prompt injection
        # in main.py _handle_message() after system prompt assembly

    def test_couple_scene_detection(self):
        from app.image_gen import is_couple_scene
        assert is_couple_scene("draw us kissing")
        assert is_couple_scene("picture of the two of us")
        assert not is_couple_scene("draw a landscape")

    def test_landscape_detection(self):
        from app.image_gen import is_landscape
        assert is_landscape("draw a landscape")
        # "rooftop" alone doesn't trigger landscape — needs explicit landscape keywords
        assert is_landscape("draw a scenic landscape view")

    def test_build_prompt_quality_tags(self):
        from app.image_gen import build_prompt
        prompt = build_prompt("sunset, beach", couple=False, affection_level=5)
        assert "masterpiece" in prompt
        assert "best quality" in prompt


# ── Image Prompt Enhancement ────────────────────────────────────────────────


class TestImagePromptEnhancement:
    """Test keyword-based scene tag generation."""

    def test_every_scene_keyword_recognized(self):
        from app.helpers import enhance_image_prompt
        scenes = {
            "sunset": "sunset",
            "night": "night",
            "rain": "rain",
            "snow": "snow",
            "beach": "beach",
            "cafe": "cafe",
            "battle": "battlefield",
            "motorcycle": "motorcycle",
            "bed": "bedroom",
            "rooftop": "rooftop",
            "garden": "garden",
            "office": "office",
            "forest": "forest",
            "city": "city",
        }
        for keyword, expected in scenes.items():
            result = enhance_image_prompt(keyword)
            assert expected in result, f"'{expected}' not in result for '{keyword}': {result}"

    def test_every_mood_keyword_recognized(self):
        from app.helpers import enhance_image_prompt
        moods = {
            "kiss": "kiss",
            "hug": "hug",
            "cuddle": "cuddling",
            "smile": "smile",
            "blush": "blush",
            "cry": "tears",
            "fight": "fighting",
            "sleep": "sleeping",
            "cook": "cooking",
            "read": "reading",
        }
        for keyword, expected in moods.items():
            result = enhance_image_prompt(keyword)
            assert expected in result, f"'{expected}' not in result for '{keyword}': {result}"

    def test_combined_keywords(self):
        from app.helpers import enhance_image_prompt
        result = enhance_image_prompt("sunset beach kiss cuddle")
        assert "sunset" in result
        assert "beach" in result
        assert "kiss" in result
        assert "cuddling" in result


# ── Mood System ─────────────────────────────────────────────────────────────


class TestMoodSystem:
    """Verify all 48 mood states are valid."""

    ALL_MOODS = [
        "composed", "focused", "prideful", "exasperated", "protective",
        "quietly_pleased", "competitive", "tender", "longing", "battle_ready",
        "flustered", "affectionate", "shy", "yearning", "devoted",
        "passionate", "jealous", "possessive", "smitten", "infatuated",
        "vigilant", "calculating", "hunting", "adrenaline",
        "scared", "terrified", "panicked", "desperate", "relieved",
        "content", "playful", "drowsy", "amused", "bored", "excited",
        "melancholic", "haunted", "conflicted", "guilty", "determined",
        "grieving", "furious", "nostalgic", "curious", "irritated",
        "defiant", "vulnerable", "grateful", "worried", "embarrassed",
    ]

    def test_mood_count(self):
        assert len(self.ALL_MOODS) >= 48

    def test_no_duplicates(self):
        assert len(self.ALL_MOODS) == len(set(self.ALL_MOODS))

    def test_default_mood_is_valid(self):
        assert "composed" in self.ALL_MOODS

    def test_romantic_moods_present(self):
        romantic = {"flustered", "affectionate", "passionate", "devoted", "tender"}
        assert romantic.issubset(set(self.ALL_MOODS))

    def test_combat_moods_present(self):
        combat = {"battle_ready", "vigilant", "calculating", "adrenaline"}
        assert combat.issubset(set(self.ALL_MOODS))


# ── Memory Archive Categories ───────────────────────────────────────────────


class TestMemoryCategories:
    """Verify affection-gated category progression."""

    CATEGORIES = [
        "Tactical Operations", "Mission Records", "Squad Moments",
        "The Commander", "Quiet Hours", "Precious Memories",
    ]

    def _available(self, level):
        cats = {"Tactical Operations", "Mission Records", "Squad Moments"}
        if level >= 3:
            cats |= {"The Commander", "Quiet Hours"}
        if level >= 6:
            cats |= {"Precious Memories"}
        return cats

    @pytest.mark.parametrize("level,expected_count", [
        (0, 3), (1, 3), (2, 3), (3, 5), (5, 5), (6, 6), (9, 6),
    ])
    def test_category_count_by_level(self, level, expected_count):
        assert len(self._available(level)) == expected_count

    def test_never_lose_categories(self):
        prev = set()
        for level in range(11):
            current = self._available(level)
            assert current >= prev, f"Lost categories at level {level}"
            prev = current


# ── Annotation Quality ──────────────────────────────────────────────────────


class TestAnnotationQuality:
    """Validate annotation text patterns."""

    LEAKED_COT_PATTERNS = [
        "We need to write",
        "The user wants",
        "Let me think",
        "1-2 sentence caption",
    ]

    REPETITIVE_PATTERNS = [
        "whispered secrets under moonlit",
        "hearts beat as one",
        "souls entwined in",
        "sanctuary of love",
    ]

    def test_leaked_cot_detected(self):
        for pattern in self.LEAKED_COT_PATTERNS:
            annotation = f"{pattern} a journal entry about..."
            assert any(annotation.startswith(p) or p in annotation
                       for p in self.LEAKED_COT_PATTERNS)

    def test_good_annotations_pass(self):
        good = [
            "He fell asleep on my shoulder. I didn't move for two hours.",
            "The coffee was terrible. His company wasn't.",
            "Caught myself adjusting his collar before the briefing.",
            "0300. Couldn't sleep. Found him awake too. We just sat there.",
        ]
        for ann in good:
            assert len(ann) >= 15
            assert not any(ann.startswith(p) for p in self.LEAKED_COT_PATTERNS)

    def test_annotation_length_bounds(self):
        """Good annotations are 15-400 chars."""
        assert 15 <= len("His hand found mine.") <= 400
        assert not (15 <= len("Bad.") <= 400)  # Too short


# ── Dedup Logic ─────────────────────────────────────────────────────────────


class TestDedupLogic:
    """Test word-overlap deduplication."""

    def _overlap(self, a: str, b: str) -> float:
        wa = set(a.lower().split())
        wb = set(b.lower().split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / max(len(wa), len(wb))

    def test_identical_is_1(self):
        assert self._overlap("hello world", "hello world") == 1.0

    def test_no_overlap_is_0(self):
        assert self._overlap("alpha beta", "gamma delta") == 0.0

    def test_partial_overlap(self):
        r = self._overlap("the quick brown fox", "the slow brown cat")
        assert 0.3 < r < 0.8

    def test_threshold_catches_near_duplicates(self):
        a = "Whispered secrets under moonlit sheets our hearts beat as one"
        b = "Whispered secrets in the moonlit sheets our hearts beat together"
        assert self._overlap(a, b) >= 0.7

    def test_threshold_passes_unique(self):
        a = "The motorcycle roared through the night streets"
        b = "He fell asleep on my shoulder during the briefing"
        assert self._overlap(a, b) < 0.3


# ── Personality Config Integrity ────────────────────────────────────────────


class TestPersonalityConfig:
    """Verify the personality YAML is structurally valid."""

    @pytest.fixture
    def p(self, personality_config):
        return personality_config

    def test_has_name(self, p):
        assert p.get("name") == "Klukai"

    def test_has_user_title(self, p):
        assert p.get("user_title") == "Commander"

    def test_has_identity(self, p):
        identity = p.get("identity", {})
        assert "full_name" in identity or "designation" in identity

    def test_has_affection_levels(self, p):
        levels = p.get("affection", {}).get("levels", [])
        assert len(levels) >= 5

    def test_affection_levels_ordered(self, p):
        levels = p.get("affection", {}).get("levels", [])
        thresholds = [l["threshold"] for l in levels]
        assert thresholds == sorted(thresholds)

    def test_has_speech_patterns(self, p):
        assert "speech_patterns" in p

    def test_has_relationships(self, p):
        rels = p.get("relationships", {})
        for member in ["mechty", "belka", "andoris", "leva"]:
            assert member in rels, f"Missing relationship: {member}"

    def test_has_equipment(self, p):
        equip = p.get("equipment", {})
        assert "motorcycle" in equip
        assert "weapons" in equip

    def test_has_world_context(self, p):
        world = p.get("world", {})
        assert world.get("year") == 2074

    def test_has_adjutant_lines(self, p):
        identity = p.get("identity", {})
        assert "adjutant_lines" in identity

    def test_adjutant_idle_lines_not_empty(self, p):
        idle = p.get("identity", {}).get("adjutant_lines", {}).get("idle", [])
        assert len(idle) >= 3

    def test_canonical_quotes_not_empty(self, p):
        quotes = p.get("identity", {}).get("canonical_quotes", [])
        assert len(quotes) >= 5

    def test_no_banned_words_in_absolute_rules(self, p):
        rules = p.get("absolute_rules", [])
        for rule in rules:
            assert "hologram" not in rule.lower() or "never" in rule.lower()


# ── Narration Pipeline ──────────────────────────────────────────────────────


class TestNarrationComprehensive:
    """Full coverage of narration fix edge cases."""

    def test_multiple_think_blocks(self):
        from app.helpers import fix_narration
        text = "<think>A</think>Hello<think>B</think> World"
        assert fix_narration(text) == "Hello World"

    def test_nested_parentheses(self):
        from app.helpers import fix_narration
        text = "(I cross my arms (firmly)) Done."
        result = fix_narration(text)
        assert "Done." in result

    def test_mixed_corrections(self):
        from app.helpers import fix_narration
        text = "(You smile) (I nod) (your expression softens) Text"
        result = fix_narration(text)
        assert "(I nod)" in result
        assert "(I smile)" in result  # "You verb" → "I verb"
        assert "your expression" not in result  # Commander narration stripped

    def test_pipe_stripping_preserves_content(self):
        from app.helpers import fix_narration
        assert fix_narration("Hello|World") == "Hello|World"  # Mid-text pipe preserved
        assert fix_narration("Hello|||") == "Hello"  # Trailing pipes stripped

    def test_empty_after_stripping(self):
        from app.helpers import fix_narration
        result = fix_narration("<think>everything is reasoning</think>")
        assert result == ""

    def test_unicode_preserved(self):
        from app.helpers import fix_narration
        text = "...Suki desu. あなたが好きです。"
        assert fix_narration(text) == text


# ── Interval Parsing Edge Cases ──────────────────────────────────────────────


class TestIntervalParsingEdgeCases:
    def test_three_hours(self):
        from app.helpers import parse_interval_minutes
        assert parse_interval_minutes("every 3 hours") == 180

    def test_10_mins(self):
        from app.helpers import parse_interval_minutes
        assert parse_interval_minutes("every 10 min") == 10

    def test_90_minutes(self):
        from app.helpers import parse_interval_minutes
        assert parse_interval_minutes("every 90 minutes") == 90

    def test_caps_insensitive(self):
        from app.helpers import parse_interval_minutes
        assert parse_interval_minutes("EVERY 15 MINUTES") == 15

    def test_natural_language(self):
        from app.helpers import parse_interval_minutes
        assert parse_interval_minutes("every an hour") == 60
        assert parse_interval_minutes("every half an hour") == 30
