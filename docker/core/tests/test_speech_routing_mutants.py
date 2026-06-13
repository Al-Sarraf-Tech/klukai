"""Behavioral / routing tests for personality.speech.

These pin the BRANCHING and ROUTING logic of the five voice-baseline
builders — NOT the prose. Per `feedback_speech_routing_bug.md`, this
module once silently routed affection levels 5-9 to the "Cold" tier;
the structural assertions below guard that whole class of off-by-one /
fall-through / comparison-operator regression.

We assert on observable STRUCTURE (which branch/tier was chosen, whether
a section is present, list length / slice caps, ordering, the empty-vs-
non-empty contract) and never on exact prompt wording — string-content
mutants are accepted residue for this prompt-text module.
"""

from __future__ import annotations

from unittest.mock import patch

from app.personality import speech
from app.personality.speech import (
    build_affection_block,
    build_character_preamble,
    build_expressive_block,
    build_japanese_block,
    build_speech_guidelines,
)


# Distinct, easy-to-detect marker tokens so a tier swap is observable
# without depending on real prose.
EXPRESSIVE_TOKENS = {
    "expressive_tokens": {
        "vocal_habits": {
            "cold_level": "MARK_COLD",
            "warm_level": "MARK_WARM",
            "tender_level": "MARK_TENDER",
        },
        "interjections": {"cat_a": ["ia1", "ia2", "ia3"], "cat_b": ["ib1", "ib2"]},
    }
}


def _expressive_tier(out: str) -> str:
    """Map the rendered expressive block back to the tier marker it chose."""
    if "MARK_COLD" in out:
        return "cold"
    if "MARK_WARM" in out:
        return "warm"
    if "MARK_TENDER" in out:
        return "tender"
    return "none"


class TestExpressiveBlockTierRouting:
    """The cold(<=1) / warm(<=3) / tender(else) if-elif-else ladder.

    Guards comparison-operator and boundary mutants (<= vs <, <= vs ==,
    swapped tiers) — exactly the bug class that once dumped high levels
    into the Cold tier.
    """

    def test_every_level_selects_expected_tier(self):
        expected = {
            0: "cold",
            1: "cold",
            2: "warm",
            3: "warm",
            4: "tender",
            5: "tender",
            6: "tender",
            7: "tender",
            8: "tender",
            9: "tender",
        }
        for lvl, tier in expected.items():
            out = build_expressive_block(EXPRESSIVE_TOKENS, lvl)
            assert _expressive_tier(out) == tier, f"level {lvl} routed to wrong tier"

    def test_high_levels_never_fall_back_to_cold(self):
        # The historical bug: levels 5-9 silently using the cold tier.
        for lvl in range(4, 10):
            out = build_expressive_block(EXPRESSIVE_TOKENS, lvl)
            assert "MARK_COLD" not in out, f"level {lvl} leaked into cold tier"
            assert "MARK_WARM" not in out, f"level {lvl} leaked into warm tier"

    def test_boundary_1_to_2_cold_then_warm(self):
        # Off-by-one on the cold/warm threshold (<=1).
        assert _expressive_tier(build_expressive_block(EXPRESSIVE_TOKENS, 1)) == "cold"
        assert _expressive_tier(build_expressive_block(EXPRESSIVE_TOKENS, 2)) == "warm"

    def test_boundary_3_to_4_warm_then_tender(self):
        # Off-by-one on the warm/tender threshold (<=3).
        assert _expressive_tier(build_expressive_block(EXPRESSIVE_TOKENS, 3)) == "warm"
        assert _expressive_tier(build_expressive_block(EXPRESSIVE_TOKENS, 4)) == "tender"

    def test_three_tiers_are_distinct(self):
        # No two adjacent tiers collapse onto the same marker.
        tiers = {
            _expressive_tier(build_expressive_block(EXPRESSIVE_TOKENS, lvl))
            for lvl in (0, 2, 4)
        }
        assert tiers == {"cold", "warm", "tender"}

    def test_negative_level_treated_as_cold(self):
        # <= 1 must include the negative side, not just == 0/1.
        assert _expressive_tier(build_expressive_block(EXPRESSIVE_TOKENS, -5)) == "cold"


class TestExpressiveBlockListHandling:
    """Interjection collection: isinstance(list) guard + slice caps."""

    def test_empty_tokens_returns_empty_string(self):
        assert build_expressive_block({}, 5) == ""
        assert build_expressive_block({"expressive_tokens": {}}, 5) == ""

    def _available_line(self, out: str) -> str:
        return next(ln for ln in out.splitlines() if "Available" in ln)

    def test_non_list_interjection_values_skipped(self):
        # isinstance(words, list) guard: a scalar category must be ignored,
        # not str-iterated into the example list.
        tokens = {
            "expressive_tokens": {
                "vocal_habits": {"cold_level": "C"},
                "interjections": {"good": ["g1", "g2"], "scalar": "IGNOREME"},
            }
        }
        line = self._available_line(build_expressive_block(tokens, 0))
        assert "g1" in line and "g2" in line
        # The scalar value must not be iterated char-by-char into the list.
        assert "IGNOREME" not in line
        shown = [w.strip() for w in line.split("Available:")[1].split(",")]
        assert shown == ["g1", "g2"]

    def test_per_category_slice_takes_first_two_only(self):
        # words[:2]: the third item of any category must be dropped.
        tokens = {
            "expressive_tokens": {
                "vocal_habits": {"cold_level": "C"},
                "interjections": {"cat": ["keep1", "keep2", "DROP_THIRD"]},
            }
        }
        line = self._available_line(build_expressive_block(tokens, 0))
        assert "keep1" in line and "keep2" in line
        assert "DROP_THIRD" not in line

    def test_overall_examples_capped_at_eight(self):
        # examples[:8]: 5 categories x 2 = 10 candidates, only 8 survive.
        tokens = {
            "expressive_tokens": {
                "vocal_habits": {"cold_level": "C"},
                "interjections": {
                    "c1": ["w1", "w2"],
                    "c2": ["w3", "w4"],
                    "c3": ["w5", "w6"],
                    "c4": ["w7", "w8"],
                    "c5": ["w9", "w10"],
                },
            }
        }
        line = self._available_line(build_expressive_block(tokens, 0))
        shown = [w.strip() for w in line.split("Available:")[1].split(",")]
        assert len(shown) == 8
        assert "w9" not in shown and "w10" not in shown


class TestJapaneseBlockFallback:
    """Exact-level lookup, then the descending fallback loop.

    Guards the `range(level-1, -1, -1)` direction and step, plus the two
    `if not phrases` guards.  Only levels 0/2/4 are defined here so the
    fallback is observable.
    """

    JP = {
        "japanese_phrases": {
            "level_0": ["jp_zero"],
            "level_2": ["jp_two"],
            "level_4": ["jp_four"],
            "note": "NOTE",
        }
    }

    def _phrase(self, out: str) -> str:
        for tag in ("jp_zero", "jp_two", "jp_four"):
            if tag in out:
                return tag
        return "none"

    def test_exact_level_used_when_defined(self):
        assert self._phrase(build_japanese_block(self.JP, 0)) == "jp_zero"
        assert self._phrase(build_japanese_block(self.JP, 2)) == "jp_two"
        assert self._phrase(build_japanese_block(self.JP, 4)) == "jp_four"

    def test_fallback_picks_nearest_lower_defined_level(self):
        # 1 -> 0, 3 -> 2 (must walk DOWNWARD, not upward).
        assert self._phrase(build_japanese_block(self.JP, 1)) == "jp_zero"
        assert self._phrase(build_japanese_block(self.JP, 3)) == "jp_two"

    def test_high_levels_fall_back_to_highest_defined_not_lowest(self):
        # Levels 5-9 must resolve to level_4, never silently to level_0.
        for lvl in range(5, 10):
            assert self._phrase(build_japanese_block(self.JP, lvl)) == "jp_four", (
                f"level {lvl} fell through to the wrong fallback"
            )

    def test_fallback_never_climbs_to_a_higher_level(self):
        # If only a high level is defined, a lower request gets nothing —
        # the loop only descends.
        jp = {"japanese_phrases": {"level_5": ["jp_five"], "note": "n"}}
        assert build_japanese_block(jp, 2) == ""

    def test_no_phrases_anywhere_returns_empty(self):
        assert build_japanese_block({}, 5) == ""
        assert build_japanese_block({"japanese_phrases": {"note": "n"}}, 5) == ""

    def test_present_block_contains_note_and_phrase_structure(self):
        out = build_japanese_block(self.JP, 4)
        assert out != ""
        assert "NOTE" in out
        # one bullet line per phrase
        assert sum(1 for ln in out.splitlines() if ln.strip().startswith("- ")) == 1


class TestSpeechGuidelinesSectionToggles:
    """Each optional section appears iff its source list is non-empty.

    Guards the four `if examples / forbidden / anti_patterns` toggles and
    their independence, plus the empty-speech short-circuit and ordering.
    """

    def _build(self, speech_cfg: dict) -> str:
        with patch.object(speech, "get_speech_patterns", return_value=speech_cfg):
            return build_speech_guidelines({}, 3)

    def test_empty_speech_returns_empty_string(self):
        assert self._build({}) == ""

    def test_header_and_tone_always_present(self):
        out = self._build({"name": "Bonded", "tone": "warm"})
        assert "CURRENT RELATIONSHIP LEVEL: Bonded" in out
        assert "SPEECH TONE:" in out

    def test_examples_section_toggles_on_content(self):
        present = self._build(
            {"name": "N", "tone": "t", "examples": ["ex_marker"]}
        )
        absent = self._build({"name": "N", "tone": "t", "examples": []})
        assert "EXAMPLE LINES" in present and "ex_marker" in present
        assert "EXAMPLE LINES" not in absent

    def test_forbidden_section_toggles_on_content(self):
        present = self._build(
            {"name": "N", "tone": "t", "forbidden": ["fb_marker"]}
        )
        absent = self._build({"name": "N", "tone": "t", "forbidden": []})
        assert "FORBIDDEN WORDS/PHRASES" in present and "fb_marker" in present
        assert "FORBIDDEN WORDS/PHRASES" not in absent

    def test_anti_patterns_section_toggles_on_content(self):
        present = self._build(
            {"name": "N", "tone": "t", "anti_patterns": ["ap_marker"]}
        )
        absent = self._build({"name": "N", "tone": "t", "anti_patterns": []})
        assert "ANTI-PATTERNS" in present and "ap_marker" in present
        assert "ANTI-PATTERNS" not in absent

    def test_sections_are_independent(self):
        # Only forbidden populated: examples + anti must stay absent.
        out = self._build(
            {"name": "N", "tone": "t", "examples": [], "forbidden": ["only_fb"], "anti_patterns": []}
        )
        assert "FORBIDDEN WORDS/PHRASES" in out
        assert "EXAMPLE LINES" not in out
        assert "ANTI-PATTERNS" not in out

    def test_section_ordering_header_tone_examples_forbidden_anti(self):
        out = self._build(
            {
                "name": "N",
                "tone": "t",
                "examples": ["e"],
                "forbidden": ["f"],
                "anti_patterns": ["a"],
            }
        )
        i_level = out.index("CURRENT RELATIONSHIP LEVEL")
        i_tone = out.index("SPEECH TONE")
        i_examples = out.index("EXAMPLE LINES")
        i_forbidden = out.index("FORBIDDEN WORDS/PHRASES")
        i_anti = out.index("ANTI-PATTERNS")
        assert i_level < i_tone < i_examples < i_forbidden < i_anti

    def test_examples_render_one_bullet_per_entry(self):
        out = self._build(
            {"name": "N", "tone": "t", "examples": ["one", "two", "three"]}
        )
        bullets = [ln for ln in out.splitlines() if ln.strip().startswith('- "')]
        assert len(bullets) == 3


class TestAffectionBlockRouting:
    """The `if modifier` toggle and the `p is None` default branch."""

    def test_modifier_present_appends_directive(self):
        p = {"affection": {"levels": [{"index": 5, "prompt_modifier": "  BE_WARM  "}]}}
        out = build_affection_block(120, 5, "Bonded", p)
        assert "AFFECTION STATE: Level 5" in out
        assert "BEHAVIORAL DIRECTIVE: BE_WARM" in out  # also confirms .strip()

    def test_blank_modifier_omits_directive_line(self):
        p = {"affection": {"levels": [{"index": 5, "prompt_modifier": "   "}]}}
        out = build_affection_block(120, 5, "Bonded", p)
        assert "AFFECTION STATE: Level 5" in out
        assert "BEHAVIORAL DIRECTIVE" not in out

    def test_missing_modifier_key_omits_directive_line(self):
        p = {"affection": {"levels": [{"index": 5}]}}
        out = build_affection_block(120, 5, "Bonded", p)
        assert "BEHAVIORAL DIRECTIVE" not in out

    def test_score_and_level_and_name_are_injected(self):
        out = build_affection_block(777, 3, "Devoted", {"affection": {"levels": []}})
        assert "Level 3" in out
        assert "Devoted" in out
        assert "777/1000" in out

    def test_none_p_loads_personality(self):
        # The `if p is None: p = load_personality()` branch.
        fake = {"affection": {"levels": [{"index": 2, "prompt_modifier": "FROM_LOAD"}]}}
        with patch.object(speech, "load_personality", return_value=fake) as load:
            out = build_affection_block(50, 2, "Trusted", None)
        assert load.called
        assert "FROM_LOAD" in out

    def test_explicit_p_does_not_load_personality(self):
        p = {"affection": {"levels": [{"index": 1, "prompt_modifier": "EXPLICIT"}]}}
        with patch.object(speech, "load_personality") as load:
            out = build_affection_block(10, 1, "Professional", p)
        assert not load.called
        assert "EXPLICIT" in out


class TestCharacterPreambleTitleRouting:
    """The `user_title` lookup + default; structural, not prose."""

    def test_default_title_when_absent(self):
        out = build_character_preamble({})
        assert "Commander" in out

    def test_custom_title_injected_and_distinct(self):
        captain = build_character_preamble({"user_title": "Captain"})
        admiral = build_character_preamble({"user_title": "Admiral"})
        assert "Captain" in captain
        assert "Admiral" in admiral
        # The injected title must actually vary the output.
        assert captain != admiral

    def test_returns_non_empty_for_all_affection_levels(self):
        # affection_level is an accepted arg but does not gate the preamble;
        # confirm no level produces an empty/short result.
        for lvl in range(0, 10):
            out = build_character_preamble({"user_title": "Commander"}, affection_level=lvl)
            assert len(out) > 50
