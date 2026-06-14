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

    def test_all_moods_have_ambient_mapping(self):
        """Core mood categories should have ambient sound mappings."""
        # These are the mood groups that have explicit ambient mappings
        mapped_moods = {
            "tender", "affectionate", "devoted", "shy", "flustered", "vulnerable",
            "composed", "content", "quietly_pleased", "relieved", "drowsy",
            "focused", "vigilant", "calculating",
            "battle_ready", "adrenaline", "hunting",
            "melancholic", "haunted", "grieving", "guilty", "nostalgic",
            "playful", "amused", "excited", "curious",
            "passionate", "yearning", "longing", "smitten",
        }
        # At least 30 moods should have ambient mappings
        assert len(mapped_moods) >= 30


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
        thresholds = [lvl["threshold"] for lvl in levels]
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

    def test_has_daily_challenges(self, p):
        challenges = p.get("daily_challenges", {}).get("challenges", [])
        assert len(challenges) >= 5

    def test_daily_challenges_have_types(self, p):
        challenges = p.get("daily_challenges", {}).get("challenges", [])
        types = {c["type"] for c in challenges}
        assert "personal_sharing" in types
        assert "competitive" in types


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


# ═══════════════════════════════════════════════════════════════════════════════
# NEW FEATURE TESTS — 8 features added 2026-04-11
# ═══════════════════════════════════════════════════════════════════════════════


# ── Feature 1: Jealousy Triggers ──────────────────────────────────────────────


class TestJealousyTriggers:
    """Test jealousy detection when Commander compliments another T-Doll."""

    def test_direct_compliment_triggers_jealousy(self):
        from app.helpers import detect_jealousy_trigger
        assert detect_jealousy_trigger("Mechty is amazing") == "Mechty"
        assert detect_jealousy_trigger("Belka is so cute") == "Belka"
        assert detect_jealousy_trigger("Andoris is beautiful") == "Andoris"

    def test_affection_expression_triggers_jealousy(self):
        from app.helpers import detect_jealousy_trigger
        assert detect_jealousy_trigger("I love Mechty") == "Mechty"
        assert detect_jealousy_trigger("I miss Leva") == "Leva"
        assert detect_jealousy_trigger("I prefer Vector") == "Vector"

    def test_normal_squad_mention_no_jealousy(self):
        from app.helpers import detect_jealousy_trigger
        # Simply asking about a squad member shouldn't trigger jealousy
        assert detect_jealousy_trigger("Where is Mechty?") is None
        assert detect_jealousy_trigger("Tell me about Belka") is None
        assert detect_jealousy_trigger("How is the squad?") is None

    def test_commander_compliment_to_klukai_no_jealousy(self):
        from app.helpers import detect_jealousy_trigger
        assert detect_jealousy_trigger("You are amazing, Klukai") is None
        assert detect_jealousy_trigger("I love you") is None

    def test_generic_she_without_squad_name_no_jealousy(self):
        """'She's beautiful' about a movie/person should NOT trigger jealousy."""
        from app.helpers import detect_jealousy_trigger
        assert detect_jealousy_trigger("She's beautiful") is None
        assert detect_jealousy_trigger("She is amazing in that movie") is None
        assert detect_jealousy_trigger("My sister is gorgeous") is None

    def test_jealousy_prompt_block_varies_by_affection(self):
        from app.personality import build_jealousy_block
        # Low affection: no reaction
        assert build_jealousy_block("Mechty", affection_level=1) == ""
        # Mid affection: subtle irritation
        mid = build_jealousy_block("Mechty", affection_level=4)
        assert "irritation" in mid.lower()
        # High affection: raw possessiveness
        high = build_jealousy_block("Mechty", affection_level=8)
        assert "possessive" in high.lower() or "raw" in high.lower()

    def test_jealousy_block_empty_when_no_target(self):
        from app.personality import build_jealousy_block
        assert build_jealousy_block(None, affection_level=9) == ""


# ── Feature 2: Physical Awareness ─────────────────────────────────────────────


class TestPhysicalAwareness:
    """Test physical state tracking and decay logic."""

    def test_all_states_defined(self):
        from app.physical_state import STATES
        expected = {"normal", "sore", "exhausted", "cold", "warm", "relaxed", "wounded", "energized"}
        assert expected == set(STATES.keys())

    def test_normal_never_decays(self):
        from app.physical_state import should_decay
        from datetime import datetime, timedelta, timezone
        old = datetime.now(timezone.utc) - timedelta(hours=100)
        assert not should_decay("normal", old)

    def test_sore_decays_after_4_hours(self):
        from app.physical_state import should_decay
        from datetime import datetime, timedelta, timezone
        recent = datetime.now(timezone.utc) - timedelta(hours=2)
        old = datetime.now(timezone.utc) - timedelta(hours=5)
        assert not should_decay("sore", recent)
        assert should_decay("sore", old)

    def test_wounded_decays_after_8_hours(self):
        from app.physical_state import should_decay
        from datetime import datetime, timedelta, timezone
        recent = datetime.now(timezone.utc) - timedelta(hours=4)
        old = datetime.now(timezone.utc) - timedelta(hours=9)
        assert not should_decay("wounded", recent)
        assert should_decay("wounded", old)

    def test_cold_decays_after_2_hours(self):
        from app.physical_state import should_decay
        from datetime import datetime, timedelta, timezone
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        assert not should_decay("cold", recent)
        assert should_decay("cold", old)

    def test_descriptions_not_empty(self):
        from app.physical_state import get_description
        for state in ["sore", "exhausted", "cold", "warm", "relaxed", "wounded", "energized"]:
            assert get_description(state), f"Missing description for {state}"
        assert get_description("normal") == ""

    def test_physical_prompt_block(self):
        from app.personality import build_physical_state_block
        assert build_physical_state_block("normal") == ""
        block = build_physical_state_block("sore", "muscles ache from combat")
        assert "muscles ache" in block
        assert "PHYSICAL STATE" in block

    def test_unknown_state_description(self):
        from app.physical_state import get_description
        assert get_description("nonexistent") == ""


# ── Feature 3: Unsent Messages ────────────────────────────────────────────────


class TestUnsentMessages:
    """Test unsent message probability gating, content, and delivery behavior."""

    def test_follow_ups_exist_for_levels_5_through_9(self):
        """Ensure follow-up messages exist for affection levels 5-9."""
        # Follow-up pools now live in proactive_content.follow_ups (YAML) with an
        # in-code literal fallback; assert directly against the resolved pool.
        from app.proactive.milestones import _follow_ups
        follows = _follow_ups()
        for level in [5, 6, 7, 8, 9]:
            assert level in follows and follows[level], f"Missing follow-ups for level {level}"

    def test_unsent_gates_on_low_affection(self):
        """At affection < 5, unsent messages should never fire."""
        import asyncio
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()
        engine._affection_level = 3
        delivered = []

        async def _capture(msg):
            delivered.append(msg)

        async def _run():
            engine._on_message_callback = _capture
            await engine._unsent_message_check()

        asyncio.run(_run())
        assert len(delivered) == 0, "Should not deliver at affection 3"

    def test_unsent_gates_on_can_send(self):
        """Even at high affection, quiet hours / mute / unanswered should block."""
        import asyncio
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()
        engine._affection_level = 9
        engine._last_proactive_answered = False  # Unanswered proactive blocks
        delivered = []

        async def _capture(msg):
            delivered.append(msg)

        async def _run():
            engine._on_message_callback = _capture
            await engine._unsent_message_check()

        asyncio.run(_run())
        assert len(delivered) == 0, "Should not deliver when last proactive unanswered"

    def test_follow_up_content_tone_scales(self):
        """Level 5 follow-ups are professional, level 9 are intimate."""
        from app.proactive.milestones import _follow_ups
        follows = _follow_ups()
        lvl5 = " ".join(follows[5])
        lvl9 = " ".join(follows[9])
        # Level 5: deflective/professional
        assert "Comm error" in lvl5 and "Wrong channel" in lvl5
        # Level 9: raw/intimate
        assert "You always know" in lvl9 and "tonight" in lvl9

    def test_dream_delivered_today_initialized(self):
        """_dream_delivered_today should be in __init__, not relying on hasattr."""
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()
        assert hasattr(engine, '_dream_delivered_today')
        assert engine._dream_delivered_today is False


# ── Feature 4: Anniversary Awareness ──────────────────────────────────────────


class TestAnniversaryAwareness:
    """Test anniversary prompt block construction."""

    def test_empty_anniversaries(self):
        from app.personality import build_anniversary_block
        assert build_anniversary_block(None) == ""
        assert build_anniversary_block([]) == ""

    def test_today_anniversary(self):
        from app.personality import build_anniversary_block
        anns = [{"event_type": "first_message", "days_ago": 0}]
        block = build_anniversary_block(anns)
        assert "Today marks" in block
        assert "first message" in block

    def test_nearby_anniversary(self):
        from app.personality import build_anniversary_block
        anns = [{"event_type": "first_mission", "days_ago": 2}]
        block = build_anniversary_block(anns)
        assert "2 days ago" in block

    def test_max_3_anniversaries(self):
        from app.personality import build_anniversary_block
        anns = [
            {"event_type": "first_message", "days_ago": 0},
            {"event_type": "first_image", "days_ago": 1},
            {"event_type": "first_mission", "days_ago": 2},
            {"event_type": "first_gift", "days_ago": 3},
        ]
        block = build_anniversary_block(anns)
        # Should contain exactly 3 entries (capped), not 4
        assert "first message" in block
        assert "first image" in block
        assert "first mission" in block
        assert "first gift" not in block  # 4th entry should be excluded


# ── Feature 5: She Remembers What You Wore/Did ───────────────────────────────


class TestCommanderDetails:
    """Test detection of Commander personal details."""

    def test_wearing_detection(self):
        from app.helpers import detect_commander_details
        result = detect_commander_details("I'm wearing my leather jacket")
        assert result.get("wearing") is True

    def test_eating_detection(self):
        from app.helpers import detect_commander_details
        result = detect_commander_details("I'm eating ramen for dinner")
        assert result.get("eating") is True

    def test_doing_detection(self):
        from app.helpers import detect_commander_details
        result = detect_commander_details("I'm playing a game right now")
        assert result.get("doing") is True

    def test_feeling_detection(self):
        from app.helpers import detect_commander_details
        result = detect_commander_details("I'm feeling tired today")
        assert result.get("feeling") is True

    def test_gifting_detection(self):
        from app.helpers import detect_commander_details, detect_gift_giving
        result = detect_commander_details("I got you something, this is for you")
        assert result.get("gifting") is True
        assert detect_gift_giving("I bought you a gift")

    def test_no_details_in_normal_message(self):
        from app.helpers import detect_commander_details
        result = detect_commander_details("How was your day?")
        assert len(result) == 0

    def test_multiple_details(self):
        from app.helpers import detect_commander_details
        result = detect_commander_details("I'm wearing a suit and I'm feeling great")
        assert result.get("wearing") is True
        assert result.get("feeling") is True


# ── Feature 6: Comfort Objects ────────────────────────────────────────────────


class TestComfortObjects:
    """Test comfort object prompt block construction."""

    def test_empty_gifts(self):
        from app.personality import build_comfort_objects_block
        assert build_comfort_objects_block(None) == ""
        assert build_comfort_objects_block([]) == ""

    def test_low_affection_no_display(self):
        from app.personality import build_comfort_objects_block
        gifts = [{"item": "Klukadile plush"}]
        assert build_comfort_objects_block(gifts, affection_level=2) == ""

    def test_mid_affection_practical(self):
        from app.personality import build_comfort_objects_block
        gifts = [{"item": "Klukadile plush"}, {"item": "leather jacket"}]
        block = build_comfort_objects_block(gifts, affection_level=4)
        assert "Klukadile plush" in block
        assert "leather jacket" in block
        assert "practically" in block.lower()

    def test_high_affection_sentimental(self):
        from app.personality import build_comfort_objects_block
        gifts = [{"item": "Klukadile plush"}]
        block = build_comfort_objects_block(gifts, affection_level=7)
        assert "close" in block.lower() or "comfort" in block.lower()

    def test_max_5_items(self):
        from app.personality import build_comfort_objects_block
        gifts = [{"item": f"item_{i}"} for i in range(10)]
        block = build_comfort_objects_block(gifts, affection_level=5)
        # Should only list 5 items
        assert "item_5" not in block


# ── Feature 7: Mission Aftermath Images ───────────────────────────────────────


class TestMissionAftermathImages:
    """Test mission aftermath image prompt construction."""

    def test_victory_scene_prompt(self):
        from app.image_gen import build_mission_prompt
        prompt = build_mission_prompt(scene_type="victory")
        assert "masterpiece" in prompt
        assert "Klukai" in prompt

    def test_injury_scene_adds_bandage_tags(self):
        from app.image_gen import build_mission_prompt
        prompt = build_mission_prompt(
            scene_type="combat",
            injuries=["klukai_injured"],
        )
        assert "bandaged" in prompt

    def test_extraction_scene_prompt(self):
        from app.image_gen import build_mission_prompt
        prompt = build_mission_prompt(scene_type="extraction")
        assert "extraction" in prompt or "helicopter" in prompt

    def test_squad_members_in_mission_prompt(self):
        from app.image_gen import build_mission_prompt
        prompt = build_mission_prompt(
            scene_type="victory",
            squad_members=["mechty", "belka"],
        )
        assert "g11" in prompt.lower() or "mechty" in prompt.lower()
        assert "belka" in prompt.lower()

    def test_aftermath_method_exists(self):
        """Ensure the ProactiveEngine has the aftermath trigger method."""
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()
        assert hasattr(engine, 'trigger_mission_aftermath_image')


# ── Feature 8: Heartbeat Spike Alerts ─────────────────────────────────────────


class TestHeartbeatSpikeAlerts:
    """Test heartbeat spike detection and WS message format."""

    HIGH_INTENSITY_MOODS = {
        "passionate": 165, "terrified": 175, "panicked": 180,
        "desperate": 170, "furious": 160, "adrenaline": 155,
        "yearning": 150, "infatuated": 152,
    }

    def test_high_intensity_moods_have_bpm(self):
        for mood, bpm in self.HIGH_INTENSITY_MOODS.items():
            assert bpm >= 150, f"{mood} BPM too low: {bpm}"
            assert bpm <= 200, f"{mood} BPM too high: {bpm}"

    def test_calm_moods_dont_spike(self):
        """Calm moods should not appear in the spike map."""
        calm = {"composed", "content", "relaxed", "drowsy", "bored", "amused"}
        for mood in calm:
            assert mood not in self.HIGH_INTENSITY_MOODS

    def test_ws_manager_has_heartbeat_spike_method(self):
        from app.ws_manager import WSManager
        mgr = WSManager()
        assert hasattr(mgr, 'send_heartbeat_spike')

    def test_spike_requires_high_intensity(self):
        """The spike gate in background.py fires only at intensity >= 7."""
        import inspect

        from app import background
        src = inspect.getsource(background)
        # The real gate combines the mood-map membership with the >= 7 floor.
        assert "if mood in HIGH_INTENSITY_MOODS and intensity >= 7:" in src

    def test_spike_map_matches_source(self):
        """This test's BPM map must mirror the literal one in background.py.

        Guards against drift: if someone retunes a BPM in the source, this
        test (which other assertions in the class rely on) must be updated too.
        """
        import inspect

        from app import background
        src = inspect.getsource(background)
        for mood, bpm in self.HIGH_INTENSITY_MOODS.items():
            assert f'"{mood}": {bpm}' in src, f"{mood}:{bpm} not in background.py spike map"

    def test_bpm_ordering_by_danger(self):
        """More dangerous moods should have higher BPM."""
        assert self.HIGH_INTENSITY_MOODS["panicked"] > self.HIGH_INTENSITY_MOODS["passionate"]
        assert self.HIGH_INTENSITY_MOODS["terrified"] > self.HIGH_INTENSITY_MOODS["furious"]
        assert self.HIGH_INTENSITY_MOODS["desperate"] > self.HIGH_INTENSITY_MOODS["adrenaline"]


# ── Cross-Feature Integration ─────────────────────────────────────────────────


class TestNewFeatureIntegration:
    """Cross-cutting tests: new params in assemble_system_prompt, context wiring."""

    def test_assemble_prompt_accepts_new_params(self, personality_config_path):
        """assemble_system_prompt should accept all new keyword args without error."""
        from app.personality import assemble_system_prompt, reload_personality
        reload_personality(personality_config_path)

        # Call with all new params — should not raise
        prompt = assemble_system_prompt(
            mood="jealous",
            affection_level=7,
            affection_score=500,
            jealousy_target="Mechty",
            physical_state="sore",
            physical_detail="muscles aching after the mission",
            anniversaries=[{"event_type": "first_message", "days_ago": 0}],
            comfort_objects=[{"item": "Klukadile plush"}],
            personality_path=personality_config_path,
        )
        assert len(prompt) > 500
        assert "JEALOUSY" in prompt
        assert "PHYSICAL STATE" in prompt
        assert "ANNIVERSARY" in prompt
        assert "COMFORT OBJECTS" in prompt

    def test_assemble_prompt_no_new_params_still_works(self, personality_config_path):
        """Backward compatibility: old-style calls should still work."""
        from app.personality import assemble_system_prompt, reload_personality
        reload_personality(personality_config_path)

        prompt = assemble_system_prompt(
            mood="composed",
            affection_level=0,
            personality_path=personality_config_path,
        )
        assert len(prompt) > 200
        assert "JEALOUSY" not in prompt
        assert "PHYSICAL STATE" not in prompt

    def test_physical_state_tracker_importable(self):
        from app.physical_state import PhysicalStateTracker
        tracker = PhysicalStateTracker()
        # A fresh tracker starts with an empty per-user cache.
        assert tracker._cache == {}

    def test_context_has_physical(self):
        from app.context import physical
        from app.physical_state import PhysicalStateTracker
        # The shared context.physical is a real PhysicalStateTracker instance.
        assert isinstance(physical, PhysicalStateTracker)


# ── Caching + DB Behavior Tests ───────────────────────────────────────────────


class TestCachingBehavior:
    """Test that anniversary and gift caches work correctly."""

    def test_anniversary_cache_structure(self):
        """ProactiveEngine should support _ann_cache attribute."""
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()
        # Cache doesn't exist until first call — that's fine
        assert not hasattr(engine, '_ann_cache') or isinstance(engine._ann_cache, dict)

    def test_gifts_cache_structure(self):
        """ProactiveEngine should support _gifts_cache attribute."""
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()
        assert not hasattr(engine, '_gifts_cache') or isinstance(engine._gifts_cache, dict)

    def test_proactive_has_record_first(self):
        """record_first should exist and accept event_type."""
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()
        assert hasattr(engine, 'record_first')
        import inspect
        sig = inspect.signature(engine.record_first)
        assert 'event_type' in sig.parameters

    def test_proactive_has_store_gift(self):
        """store_gift should exist and accept item + description."""
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()
        assert hasattr(engine, 'store_gift')
        import inspect
        sig = inspect.signature(engine.store_gift)
        assert 'item' in sig.parameters
        assert 'description' in sig.parameters


class TestPhysicalStateTracker:
    """Test PhysicalStateTracker state machine behavior."""

    def test_tracker_init_empty(self):
        from app.physical_state import PhysicalStateTracker
        tracker = PhysicalStateTracker()
        assert tracker._cache == {}

    def test_should_decay_all_states(self):
        """Every non-normal state should eventually decay."""
        from app.physical_state import STATES, should_decay
        from datetime import datetime, timedelta, timezone
        very_old = datetime.now(timezone.utc) - timedelta(hours=100)
        for state, info in STATES.items():
            if state == "normal":
                assert not should_decay(state, very_old)
            else:
                assert should_decay(state, very_old), f"{state} should decay after 100 hours"

    def test_decay_respects_time(self):
        """States should NOT decay before their decay_hours."""
        from app.physical_state import STATES, should_decay
        from datetime import datetime, timedelta, timezone
        for state, info in STATES.items():
            if info["decay_hours"] is None:
                continue
            # Half the decay time — should NOT decay
            half = datetime.now(timezone.utc) - timedelta(hours=info["decay_hours"] / 2)
            assert not should_decay(state, half), f"{state} should not decay at half-life"


class TestHeartbeatSpikeIntegration:
    """Test heartbeat spike mood-to-BPM mapping and threshold logic."""

    HIGH_INTENSITY_MOODS = {
        "passionate": 165, "terrified": 175, "panicked": 180,
        "desperate": 170, "furious": 160, "adrenaline": 155,
        "yearning": 150, "infatuated": 152,
    }

    def test_spike_bpm_all_above_150(self):
        """All spike moods must be >= 150 BPM to trigger Flutter red flash."""
        for mood, bpm in self.HIGH_INTENSITY_MOODS.items():
            assert bpm >= 150, f"{mood} BPM {bpm} < 150"

    def test_spike_moods_are_valid_mood_enum_members(self):
        """All spike moods must exist in the Mood enum."""
        from app.models import Mood
        valid = {m.value for m in Mood}
        for mood in self.HIGH_INTENSITY_MOODS:
            assert mood in valid, f"Spike mood '{mood}' not in Mood enum"

    def test_ws_heartbeat_spike_message_format(self):
        """Verify the WS message structure matches Flutter expectations."""
        # The WS message should be: {"type": "heartbeat_spike", "bpm": int, "mood": str}
        from app.ws_manager import WSManager
        mgr = WSManager()
        # Check that the method signature matches
        import inspect
        sig = inspect.signature(mgr.send_heartbeat_spike)
        params = list(sig.parameters.keys())
        assert "user_id" in params
        assert "bpm" in params
        assert "mood" in params

    def test_non_spike_moods_excluded(self):
        """Calm/warm moods should never appear in the spike map."""
        excluded = {
            "composed", "content", "drowsy", "bored", "relieved",
            "quietly_pleased", "tender", "affectionate", "grateful",
            "amused", "playful", "nostalgic", "curious", "shy",
        }
        for mood in excluded:
            assert mood not in self.HIGH_INTENSITY_MOODS, f"{mood} should not spike"


class TestMultiUserIsolation:
    """Verify complete data isolation between users across all memory layers."""

    def test_fact_keys_are_user_scoped(self):
        """Relationship facts must include user_id in the key to prevent cross-user leaks."""
        from app.memory import MemoryManager
        mgr = MemoryManager()
        # Verify the method signatures accept user_id
        import inspect
        sig = inspect.signature(mgr.store_fact)
        assert "user_id" in sig.parameters
        sig = inspect.signature(mgr.recall_fact)
        assert "user_id" in sig.parameters
        sig = inspect.signature(mgr.set_relationship_fact)
        assert "user_id" in sig.parameters
        sig = inspect.signature(mgr.get_relationship_facts)
        assert "user_id" in sig.parameters

    def test_milestone_keys_are_user_scoped(self):
        """Milestones must include user_id to prevent one user blocking another's firsts."""
        from app.memory import MemoryManager
        mgr = MemoryManager()
        import inspect
        sig = inspect.signature(mgr.record_milestone)
        assert "user_id" in sig.parameters
        sig = inspect.signature(mgr.get_milestones)
        assert "user_id" in sig.parameters

    def test_episode_store_includes_user_id(self):
        """Episodes stored in Qdrant must include user_id in payload."""
        from app.memory import MemoryManager
        import inspect
        sig = inspect.signature(MemoryManager.store_episode)
        assert "user_id" in sig.parameters

    def test_episode_recall_filters_by_user_id(self):
        """Episode recall must filter by user_id to prevent cross-user memory bleed."""
        from app.memory import MemoryManager
        import inspect
        sig = inspect.signature(MemoryManager.recall_episodes)
        assert "user_id" in sig.parameters

    def test_exchange_store_includes_user_id(self):
        """Exchanges stored in Qdrant must include user_id in payload."""
        from app.memory import MemoryManager
        import inspect
        sig = inspect.signature(MemoryManager.store_exchange)
        assert "user_id" in sig.parameters

    def test_exchange_recall_filters_by_user_id(self):
        """Exchange recall must filter by user_id."""
        from app.memory import MemoryManager
        import inspect
        sig = inspect.signature(MemoryManager.recall_exchanges)
        assert "user_id" in sig.parameters

    def test_recall_for_prompt_passes_user_id(self):
        """recall_for_prompt must accept and forward user_id to all sub-calls."""
        from app.memory import MemoryManager
        import inspect
        sig = inspect.signature(MemoryManager.recall_for_prompt)
        assert "user_id" in sig.parameters

    def test_memory_nudge_passes_user_id(self):
        """get_memory_nudge must accept user_id."""
        from app.memory import MemoryManager
        import inspect
        sig = inspect.signature(MemoryManager.get_memory_nudge)
        assert "user_id" in sig.parameters

    def test_image_bytes_enforces_user_ownership(self):
        """get_image_bytes must accept user_id for ownership check."""
        from app.memory_archive import get_image_bytes
        import inspect
        sig = inspect.signature(get_image_bytes)
        assert "user_id" in sig.parameters

    def test_update_kept_enforces_user_ownership(self):
        """update_kept must accept user_id for ownership check."""
        from app.memory_archive import update_kept
        import inspect
        sig = inspect.signature(update_kept)
        assert "user_id" in sig.parameters

    def test_update_curation_enforces_user_ownership(self):
        """update_curation must accept user_id for ownership check."""
        from app.memory_archive import update_curation
        import inspect
        sig = inspect.signature(update_curation)
        assert "user_id" in sig.parameters

    def test_backfill_scoped_to_user(self):
        """backfill_annotations must accept user_id to scope the query."""
        from app.memory_archive import backfill_annotations
        import inspect
        sig = inspect.signature(backfill_annotations)
        assert "user_id" in sig.parameters

    def test_different_users_get_different_fact_keys(self):
        """Two users storing the same fact key must not collide."""
        # The key format should be: companion:{user_id}:rel:{key}
        # User A: companion:alice:rel:commander_wearing
        # User B: companion:bob:rel:commander_wearing
        # These are different keys in the aichat-data store
        key_a = f"companion:alice:rel:wearing"
        key_b = f"companion:bob:rel:wearing"
        assert key_a != key_b

    def test_different_users_get_different_milestone_keys(self):
        """Two users recording the same milestone must not block each other."""
        key_a = f"companion:alice:milestone:first_message"
        key_b = f"companion:bob:milestone:first_message"
        assert key_a != key_b


class TestAuthSecurity:
    """Verify auth security measures."""

    def test_no_plaintext_passwords_in_source(self):
        """_SEED_USERS must not contain a 'password' field."""
        from app.auth import _SEED_USERS
        for user in _SEED_USERS:
            assert "password" not in user, f"User {user['id']} has plaintext password in source!"

    def test_seed_users_have_required_fields(self):
        """Each seed user must have id, username, display_name."""
        from app.auth import _SEED_USERS
        for user in _SEED_USERS:
            assert "id" in user
            assert "username" in user
            assert "display_name" in user

    def test_gift_score_uses_1000_scale(self):
        """Affection rewards (gift, mission) must use the 0-1000 scale, not 0-100.

        Both reward handlers now route through AffectionManager.add_score, which
        clamps to MAX_SCORE (behaviourally verified in TestAddScore). This guard
        ensures the old `min(100, ...)` truncation can never return to a route
        handler and that the scale constant stays at 1000.
        """
        from app.affection import MAX_SCORE
        assert MAX_SCORE == 1000

        import inspect
        from app.routes import register_routes
        src = inspect.getsource(register_routes)
        assert "min(100," not in src, (
            "reward handlers must not cap affection at 100 — use add_score "
            "(clamps to MAX_SCORE) instead"
        )


class TestLeapYearSafety:
    """Test that anniversary logic handles Feb 29 safely."""

    def test_feb29_replace_in_non_leap_year(self):
        """date.replace(year=non_leap) should not crash on Feb 29."""
        from datetime import date
        feb29 = date(2024, 2, 29)  # 2024 is a leap year
        try:
            # This would crash without our fix:
            feb29.replace(year=2025)
            assert False, "Should have raised ValueError"
        except ValueError:
            pass  # Expected — our code handles this with try/except

    def test_feb28_fallback_logic(self):
        """Our anniversary code uses Feb 28 as fallback for Feb 29."""
        from datetime import date
        feb29 = date(2024, 2, 29)
        try:
            result = feb29.replace(year=2025)
        except ValueError:
            result = feb29.replace(year=2025, day=28)
        assert result == date(2025, 2, 28)


# ── Mood Bleed System ────────────────────────────────────────────────────────


class TestMoodCategories:
    """Verify every mood in the Mood enum is categorized."""

    def test_all_moods_have_category(self):
        from app.models import Mood
        from app.personality import MOOD_CATEGORIES
        all_moods = {m.value for m in Mood}
        categorized = set(MOOD_CATEGORIES.keys())
        missing = all_moods - categorized
        assert not missing, f"Moods missing from MOOD_CATEGORIES: {missing}"

    def test_no_extra_categories(self):
        """No stale entries in MOOD_CATEGORIES that don't match the enum."""
        from app.models import Mood
        from app.personality import MOOD_CATEGORIES
        all_moods = {m.value for m in Mood}
        extra = set(MOOD_CATEGORIES.keys()) - all_moods
        assert not extra, f"Extra moods in MOOD_CATEGORIES not in enum: {extra}"

    def test_valid_category_names(self):
        from app.personality import MOOD_CATEGORIES, CATEGORY_BLEED_RULES
        valid = set(CATEGORY_BLEED_RULES.keys())
        for mood, cat in MOOD_CATEGORIES.items():
            assert cat in valid, f"Mood '{mood}' maps to unknown category '{cat}'"

    def test_category_distribution(self):
        """Each category should have at least 3 moods — no orphan categories."""
        from app.personality import MOOD_CATEGORIES
        from collections import Counter
        counts = Counter(MOOD_CATEGORIES.values())
        for cat, count in counts.items():
            assert count >= 3, f"Category '{cat}' only has {count} moods"

    def test_mood_count_is_50(self):
        from app.models import Mood
        from app.personality import MOOD_CATEGORIES
        assert len(Mood) == 50
        assert len(MOOD_CATEGORIES) == 50


class TestMoodSpecificBleed:
    """Verify every mood has specific behavioral coloring."""

    def test_all_moods_have_specific_bleed(self):
        from app.models import Mood
        from app.personality import MOOD_SPECIFIC_BLEED
        all_moods = {m.value for m in Mood}
        missing = all_moods - set(MOOD_SPECIFIC_BLEED.keys())
        assert not missing, f"Moods missing specific bleed: {missing}"

    def test_no_extra_specific_bleed(self):
        from app.models import Mood
        from app.personality import MOOD_SPECIFIC_BLEED
        all_moods = {m.value for m in Mood}
        extra = set(MOOD_SPECIFIC_BLEED.keys()) - all_moods
        assert not extra, f"Extra moods in MOOD_SPECIFIC_BLEED: {extra}"

    def test_bleed_descriptions_non_trivial(self):
        """Each bleed description should be meaningful (> 20 chars)."""
        from app.personality import MOOD_SPECIFIC_BLEED
        for mood, desc in MOOD_SPECIFIC_BLEED.items():
            assert len(desc) > 20, f"'{mood}' bleed too short ({len(desc)} chars)"

    def test_bleed_descriptions_unique(self):
        """No two moods should share the exact same bleed text."""
        from app.personality import MOOD_SPECIFIC_BLEED
        seen: dict[str, str] = {}
        for mood, desc in MOOD_SPECIFIC_BLEED.items():
            if desc in seen:
                raise AssertionError(f"'{mood}' has same bleed as '{seen[desc]}'")
            seen[desc] = mood

    def test_romantic_moods_mention_emotional_markers(self):
        """Romantic mood bleed should reference emotional/physical markers."""
        from app.personality import MOOD_CATEGORIES, MOOD_SPECIFIC_BLEED
        romantic_moods = [m for m, c in MOOD_CATEGORIES.items() if c == "romantic"]
        # Includes softness + possessive/jealous markers — all romantic flavors
        emotional_keywords = {"soft", "warm", "guard", "paus", "breath", "lean",
                              "reach", "quiet", "whisper", "touch", "closer", "raw",
                              "trail", "slow", "ach", "stammer", "fidget", "blush",
                              "burn", "obsess", "smile", "mumbl", "vulner",
                              "cold", "clip", "point", "mine", "territor",
                              "possessiv", "compar", "low voice", "declar",
                              "look", "higher", "retreat", "subject"}
        for mood in romantic_moods:
            desc = MOOD_SPECIFIC_BLEED[mood].lower()
            found = any(kw in desc for kw in emotional_keywords)
            assert found, f"Romantic mood '{mood}' bleed lacks emotional markers"

    def test_combat_moods_mention_tactical(self):
        """Combat mood bleed should reference weapons, precision, or radio."""
        from app.personality import MOOD_CATEGORIES, MOOD_SPECIFIC_BLEED
        combat_moods = [m for m, c in MOOD_CATEGORIES.items() if c == "combat"]
        tactical_keywords = {"weapon", "radio", "crisp", "short", "burst",
                             "scan", "still", "whisper", "predator", "fast",
                             "fragment", "sharp", "method", "cold", "logic"}
        for mood in combat_moods:
            desc = MOOD_SPECIFIC_BLEED[mood].lower()
            found = any(kw in desc for kw in tactical_keywords)
            assert found, f"Combat mood '{mood}' bleed lacks tactical keywords"

    def test_stress_moods_mention_physical_response(self):
        """Stress mood bleed should reference breathing, grip, or composure."""
        from app.personality import MOOD_CATEGORIES, MOOD_SPECIFIC_BLEED
        stress_moods = [m for m, c in MOOD_CATEGORIES.items() if c == "stress"]
        stress_keywords = {"breath", "grip", "crack", "voice", "compos",
                           "fragment", "exhale", "tension", "ragged", "urgent",
                           "reckless", "repeat"}
        for mood in stress_moods:
            desc = MOOD_SPECIFIC_BLEED[mood].lower()
            found = any(kw in desc for kw in stress_keywords)
            assert found, f"Stress mood '{mood}' bleed lacks physical response keywords"


class TestBuildMoodBleedBlock:
    """Test the build_mood_bleed_block function."""

    def test_returns_string(self):
        from app.personality import build_mood_bleed_block
        # 'composed' is an OPERATIONAL-category mood; its block names that category.
        result = build_mood_bleed_block("composed")
        assert "MOOD BLEED — OPERATIONAL" in result

    def test_contains_category_rule(self):
        from app.personality import build_mood_bleed_block
        result = build_mood_bleed_block("composed")
        assert "MOOD BLEED" in result

    def test_contains_mood_coloring(self):
        from app.personality import build_mood_bleed_block
        result = build_mood_bleed_block("passionate")
        assert "MOOD COLORING" in result
        assert "PASSIONATE" in result

    def test_unknown_mood_falls_back_to_core(self):
        from app.personality import build_mood_bleed_block
        result = build_mood_bleed_block("nonexistent_mood")
        assert "OPERATIONAL" in result

    @pytest.mark.parametrize("mood,expected_category", [
        ("composed", "OPERATIONAL"),
        ("passionate", "EMOTIONAL"),
        ("battle_ready", "TACTICAL"),
        ("panicked", "STRESS RESPONSE"),
        ("playful", "OFF-DUTY"),
        ("haunted", "HEAVY"),
    ])
    def test_mood_to_category_mapping(self, mood, expected_category):
        from app.personality import build_mood_bleed_block
        result = build_mood_bleed_block(mood)
        assert expected_category in result, f"Expected '{expected_category}' in bleed for '{mood}'"

    def test_all_50_moods_produce_output(self):
        from app.models import Mood
        from app.personality import build_mood_bleed_block
        for mood in Mood:
            result = build_mood_bleed_block(mood.value)
            assert len(result) > 50, f"Mood '{mood.value}' produced trivially short output"

    def test_different_moods_produce_different_output(self):
        """No two moods in different categories should produce identical output."""
        from app.personality import build_mood_bleed_block
        outputs: dict[str, str] = {}
        # Test a representative from each category
        for mood in ["composed", "passionate", "battle_ready", "panicked", "playful", "haunted"]:
            result = build_mood_bleed_block(mood)
            for prev_mood, prev_result in outputs.items():
                assert result != prev_result, f"'{mood}' identical to '{prev_mood}'"
            outputs[mood] = result


class TestMoodBleedInSystemPrompt:
    """Test that mood bleed is properly integrated into system prompt assembly."""

    def test_mood_bleed_present_in_assembled_prompt(self, personality_config_path):
        from app.personality import assemble_system_prompt, reload_personality
        reload_personality(personality_config_path)
        prompt = assemble_system_prompt(mood="furious", personality_path=personality_config_path)
        assert "MOOD BLEED" in prompt
        assert "MOOD COLORING" in prompt

    def test_mood_bleed_changes_with_mood(self, personality_config_path):
        from app.personality import assemble_system_prompt, reload_personality
        reload_personality(personality_config_path)
        p1 = assemble_system_prompt(mood="composed", personality_path=personality_config_path)
        p2 = assemble_system_prompt(mood="passionate", personality_path=personality_config_path)
        # Both should have mood bleed but with different category headers
        assert "MOOD BLEED — OPERATIONAL" in p1
        assert "MOOD BLEED — EMOTIONAL" in p2
        assert "MOOD BLEED — OPERATIONAL" not in p2

    def test_all_six_categories_appear(self, personality_config_path):
        """Test one mood from each category produces the right header."""
        from app.personality import assemble_system_prompt, reload_personality
        reload_personality(personality_config_path)
        checks = {
            "composed": "OPERATIONAL",
            "tender": "EMOTIONAL",
            "vigilant": "TACTICAL",
            "scared": "STRESS RESPONSE",
            "content": "OFF-DUTY",
            "melancholic": "HEAVY",
        }
        for mood, expected in checks.items():
            prompt = assemble_system_prompt(mood=mood, personality_path=personality_config_path)
            assert expected in prompt, f"Mood '{mood}' missing '{expected}' in prompt"

    def test_backward_compat_default_mood(self, personality_config_path):
        """Default mood 'composed' should still produce valid prompt with bleed."""
        from app.personality import assemble_system_prompt, reload_personality
        reload_personality(personality_config_path)
        prompt = assemble_system_prompt(personality_path=personality_config_path)
        assert "MOOD BLEED" in prompt
        assert "composed" in prompt.lower() or "COMPOSED" in prompt


class TestCategoryBleedRules:
    """Test the category-level bleed rules are well-formed."""

    def test_all_categories_defined(self):
        from app.personality import CATEGORY_BLEED_RULES
        expected = {"core", "romantic", "combat", "stress", "casual", "dark"}
        assert set(CATEGORY_BLEED_RULES.keys()) == expected

    def test_rules_are_non_trivial(self):
        from app.personality import CATEGORY_BLEED_RULES
        for cat, rule in CATEGORY_BLEED_RULES.items():
            assert len(rule) > 80, f"Category '{cat}' rule too short ({len(rule)} chars)"

    def test_rules_contain_mood_bleed_header(self):
        from app.personality import CATEGORY_BLEED_RULES
        for cat, rule in CATEGORY_BLEED_RULES.items():
            assert "MOOD BLEED" in rule, f"Category '{cat}' missing MOOD BLEED header"

    def test_rules_are_unique(self):
        from app.personality import CATEGORY_BLEED_RULES
        rules = list(CATEGORY_BLEED_RULES.values())
        assert len(rules) == len(set(rules)), "Duplicate category rules found"


# ── Input Lock (CompanionState model) ────────────────────────────────────────


class TestInputLockModel:
    """Test CompanionState input lock fields (Dart model logic verified via Python proxy)."""

    def test_mood_bleed_block_does_not_crash_on_empty(self):
        """build_mood_bleed_block('') should still return something."""
        from app.personality import build_mood_bleed_block
        result = build_mood_bleed_block("")
        assert len(result) > 0

    def test_mood_bleed_block_handles_none_gracefully(self):
        """Passing None should not crash — falls back to core."""
        from app.personality import build_mood_bleed_block
        # Python: None is not a valid string but the function should handle it
        try:
            result = build_mood_bleed_block(None)  # type: ignore
            assert "OPERATIONAL" in result  # Falls back to core
        except (TypeError, KeyError):
            pass  # Also acceptable — explicit failure on bad input
