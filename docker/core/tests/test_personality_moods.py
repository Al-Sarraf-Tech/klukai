"""Tests for personality.moods — mood taxonomy, bleed rules, context block.

Currently at 60% coverage. Targets the branches in build_context_block
(time-of-day × affection-level matrix) + build_mood_bleed_block edge cases.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from app.personality.moods import (
    CATEGORY_BLEED_RULES,
    MOOD_CATEGORIES,
    MOOD_SPECIFIC_BLEED,
    build_context_block,
    build_mood_bleed_block,
)


class TestMoodTaxonomy:
    def test_all_moods_have_known_category(self):
        valid = set(CATEGORY_BLEED_RULES.keys())
        for mood, cat in MOOD_CATEGORIES.items():
            assert cat in valid, f"{mood} → {cat} is not a known category"

    def test_categories_cover_six_buckets(self):
        assert set(CATEGORY_BLEED_RULES.keys()) == {
            "core", "romantic", "combat", "stress", "casual", "dark",
        }

    def test_specific_bleed_covers_all_moods(self):
        for mood in MOOD_CATEGORIES:
            assert mood in MOOD_SPECIFIC_BLEED, f"{mood} missing from MOOD_SPECIFIC_BLEED"


class TestMoodBleedBlock:
    def test_known_mood_includes_category_rule_and_specific(self):
        out = build_mood_bleed_block("composed")
        assert "MOOD BLEED — OPERATIONAL" in out
        assert "MOOD COLORING — COMPOSED" in out

    def test_unknown_mood_falls_back_to_core(self):
        out = build_mood_bleed_block("bogus_mood_that_does_not_exist")
        assert "MOOD BLEED — OPERATIONAL" in out
        # No specific coloring for unknown mood
        assert "MOOD COLORING" not in out

    def test_romantic_mood_uses_romantic_rule(self):
        out = build_mood_bleed_block("tender")
        assert "MOOD BLEED — EMOTIONAL" in out

    def test_combat_mood_uses_combat_rule(self):
        out = build_mood_bleed_block("battle_ready")
        assert "MOOD BLEED — TACTICAL" in out

    def test_stress_mood_uses_stress_rule(self):
        out = build_mood_bleed_block("panicked")
        assert "MOOD BLEED — STRESS RESPONSE" in out

    def test_casual_mood_uses_casual_rule(self):
        out = build_mood_bleed_block("playful")
        assert "MOOD BLEED — OFF-DUTY" in out

    def test_dark_mood_uses_dark_rule(self):
        out = build_mood_bleed_block("haunted")
        assert "MOOD BLEED — HEAVY" in out

    def test_mood_coloring_uppercases_and_replaces_underscores(self):
        out = build_mood_bleed_block("quietly_pleased")
        assert "MOOD COLORING — QUIETLY PLEASED" in out


class TestContextBlock:
    def _at_hour(self, hour: int) -> datetime:
        return datetime(2026, 5, 16, hour, 0, 0)

    def test_morning_window(self):
        with patch("app.personality.moods.now_local") as mock_dt:
            mock_dt.return_value = self._at_hour(8)
            mock_dt.strftime = datetime.strftime
            out = build_context_block(mood="composed", affection_level=2)
        assert "morning operational window" in out
        assert "Blazing Star" in out

    def test_afternoon_window(self):
        with patch("app.personality.moods.now_local") as mock_dt:
            mock_dt.return_value = self._at_hour(14)
            out = build_context_block(mood="composed")
        assert "afternoon operations" in out

    def test_evening_high_affection_relaxes_outfit(self):
        with patch("app.personality.moods.now_local") as mock_dt:
            mock_dt.return_value = self._at_hour(19)
            out = build_context_block(mood="composed", affection_level=3)
        assert "evening wind-down" in out
        assert "Light tactical" in out

    def test_evening_low_affection_keeps_full_tactical(self):
        with patch("app.personality.moods.now_local") as mock_dt:
            mock_dt.return_value = self._at_hour(19)
            out = build_context_block(mood="composed", affection_level=0)
        assert "Blazing Star" in out

    def test_late_night_high_affection_dorm_mode(self):
        with patch("app.personality.moods.now_local") as mock_dt:
            mock_dt.return_value = self._at_hour(23)
            out = build_context_block(mood="composed", affection_level=5)
        assert "Dorm casual" in out
        assert "softer in these hours" in out

    def test_late_night_mid_affection_light_tactical(self):
        with patch("app.personality.moods.now_local") as mock_dt:
            mock_dt.return_value = self._at_hour(23)
            out = build_context_block(mood="composed", affection_level=2)
        assert "Light tactical — off-duty" in out

    def test_late_night_low_affection_full_gear(self):
        with patch("app.personality.moods.now_local") as mock_dt:
            mock_dt.return_value = self._at_hour(23)
            out = build_context_block(mood="composed", affection_level=0)
        assert "Full tactical gear" in out

    def test_days_together_first_day(self):
        out = build_context_block(mood="composed", days_together=1)
        assert "first day together" in out

    def test_days_together_one_week(self):
        out = build_context_block(mood="composed", days_together=7)
        assert "One week together" in out

    def test_days_together_one_month(self):
        out = build_context_block(mood="composed", days_together=30)
        assert "One month together" in out

    def test_days_together_multi_month(self):
        out = build_context_block(mood="composed", days_together=60)
        assert "2 months together" in out

    def test_days_together_generic(self):
        out = build_context_block(mood="composed", days_together=15)
        assert "15 days" in out

    def test_zero_days_no_date_line(self):
        out = build_context_block(mood="composed", days_together=0)
        assert "DAYS WITH COMMANDER" not in out

    def test_includes_mood_emotional_state(self):
        out = build_context_block(mood="tender", affection_level=4)
        assert "EMOTIONAL STATE: tender" in out
