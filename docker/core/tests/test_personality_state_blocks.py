"""Tests for personality.state_blocks — physical, jealousy, anniversary, comfort, mission.

Currently at 95% coverage; this covers the remaining edge branches.
"""

from __future__ import annotations

from app.personality.state_blocks import (
    build_anniversary_block,
    build_comfort_objects_block,
    build_jealousy_block,
    build_mission_context_block,
    build_physical_state_block,
)


class TestPhysicalStateBlock:
    def test_normal_returns_empty(self):
        assert build_physical_state_block("normal") == ""

    def test_empty_string_returns_empty(self):
        assert build_physical_state_block("") == ""

    def test_custom_detail_used(self):
        out = build_physical_state_block("tired", "sore from training")
        assert "sore from training" in out

    def test_falls_back_to_state_when_no_detail(self):
        out = build_physical_state_block("tired")
        assert "tired" in out


class TestJealousyBlock:
    def test_no_target_returns_empty(self):
        assert build_jealousy_block(None) == ""

    def test_low_affection_too_cold_to_care(self):
        # affection 0-2 returns empty
        assert build_jealousy_block("Mechty", affection_level=0) == ""
        assert build_jealousy_block("Mechty", affection_level=2) == ""

    def test_mid_affection_subtle_irritation(self):
        out = build_jealousy_block("Mechty", affection_level=3)
        assert "subtle coolness" in out
        assert "Mechty" in out

    def test_high_mid_affection_competitive_edge(self):
        out = build_jealousy_block("Belka", affection_level=5)
        assert "competitive edge" in out
        assert "elite" in out.lower() or "ELITE" in out

    def test_very_high_affection_vulnerable(self):
        out = build_jealousy_block("Andoris", affection_level=8)
        assert "vulnerable" in out
        assert "I'm the only one you need" in out

    def test_boundary_3_to_4_uses_subtle(self):
        assert "subtle coolness" in build_jealousy_block("X", 3)
        assert "subtle coolness" in build_jealousy_block("X", 4)

    def test_boundary_5_to_6_uses_competitive(self):
        assert "competitive edge" in build_jealousy_block("X", 5)
        assert "competitive edge" in build_jealousy_block("X", 6)


class TestAnniversaryBlock:
    def test_none_returns_empty(self):
        assert build_anniversary_block(None) == ""

    def test_empty_list_returns_empty(self):
        assert build_anniversary_block([]) == ""

    def test_today_marks_anniversary(self):
        out = build_anniversary_block([{"days_ago": 0, "event_type": "first_meeting"}])
        assert "Today marks" in out
        assert "first meeting" in out

    def test_within_three_days(self):
        out = build_anniversary_block([{"days_ago": 2, "event_type": "first_kiss"}])
        assert "2 days ago" in out
        assert "first kiss" in out

    def test_caps_at_three_entries(self):
        anns = [
            {"days_ago": i, "event_type": f"event_{i}"}
            for i in range(5)
        ]
        out = build_anniversary_block(anns)
        # event_type has underscores replaced with spaces in output
        assert "event 0" in out
        assert "event 2" in out
        # event_3 and event_4 should be dropped
        assert "event 4" not in out

    def test_old_anniversaries_not_listed(self):
        # >3 days ago doesn't produce a line
        out = build_anniversary_block([{"days_ago": 30, "event_type": "old_event"}])
        # Header is present but the entry is filtered out (underscores → spaces in output)
        assert "old event" not in out


class TestComfortObjectsBlock:
    def test_none_returns_empty(self):
        assert build_comfort_objects_block(None, affection_level=5) == ""

    def test_low_affection_returns_empty(self):
        # Below 3 = no comfort object reference
        assert build_comfort_objects_block([{"item": "plushie"}], affection_level=2) == ""

    def test_mid_affection_practical_acknowledgment(self):
        out = build_comfort_objects_block([{"item": "watch"}], affection_level=3)
        assert "practically" in out
        assert "watch" in out

    def test_high_affection_emotional_acknowledgment(self):
        out = build_comfort_objects_block([{"item": "ring"}], affection_level=7)
        assert "keeping these close" in out

    def test_caps_at_five_items(self):
        gifts = [{"item": f"item_{i}"} for i in range(8)]
        out = build_comfort_objects_block(gifts, affection_level=5)
        assert "item_0" in out
        assert "item_4" in out
        assert "item_7" not in out


class TestMissionContextBlock:
    def test_no_mission_returns_empty(self):
        assert build_mission_context_block(None) == ""

    def test_empty_string_returns_empty(self):
        assert build_mission_context_block("") == ""

    def test_describes_active_deployment(self):
        out = build_mission_context_block("Sortie to Zone 6")
        assert "ACTIVE MISSION" in out
        assert "Sortie to Zone 6" in out
        assert "radio transmissions" in out
        assert "Elmo" in out


class TestAnniversaryBlockPrincessUpgrade:
    def test_upcoming_anniversary_uses_future_tense(self):
        from app.personality.state_blocks import build_anniversary_block
        out = build_anniversary_block([{"days_ago": -2, "event_type": "first_mission", "years_ago": 1}])
        assert "In 2 days" in out
        assert "first mission" in out
        assert "ago" not in out.split("first mission")[0][-40:]  # not past tense for this line

    def test_past_near_anniversary_uses_ago(self):
        from app.personality.state_blocks import build_anniversary_block
        out = build_anniversary_block([{"days_ago": 2, "event_type": "first_gift"}])
        assert "2 days ago" in out
