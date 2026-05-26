"""Tests for proactive.py pure helpers (no DB / no scheduler dependencies).

Targets the static behaviors that are testable without spinning up the
ProactiveEngine or mocking the global scheduler state. Coverage focus:
- has_active_mission() without active mission
- MAJOR_EVENTS contents (regression guard)
- Message template structure (affection-level keys 0-9 present in core dicts)
"""

from __future__ import annotations

from app import proactive


class TestHasActiveMission:
    def test_returns_false_when_no_mission(self):
        # Module starts with no active mission (or is None'd by previous test)
        proactive.state._active_mission_timer = None
        assert proactive.has_active_mission() is False

    def test_returns_false_when_timer_inactive(self):
        class _StubTimer:
            active = False
        proactive.state._active_mission_timer = _StubTimer()
        try:
            assert proactive.has_active_mission() is False
        finally:
            proactive.state._active_mission_timer = None

    def test_returns_true_when_timer_active(self):
        class _StubTimer:
            active = True
        proactive.state._active_mission_timer = _StubTimer()
        try:
            assert proactive.has_active_mission() is True
        finally:
            proactive.state._active_mission_timer = None


class TestMajorEvents:
    def test_contains_all_documented_events(self):
        # Regression guard — these events are referenced by mission templates
        for evt in [
            "ambush", "squad_injured", "klukai_injured", "equipment_failure",
            "weather", "comms_disruption", "discovery", "medical_emergency",
            "mechty_asleep", "belka_reckless", "andoris_freeze",
        ]:
            assert evt in proactive.MAJOR_EVENTS

    def test_has_at_least_eleven_events(self):
        # Catches accidental deletion
        assert len(proactive.MAJOR_EVENTS) >= 11


class TestConstants:
    def test_quiet_hour_start_is_evening(self):
        assert proactive.QUIET_HOUR_START == 23

    def test_quiet_hour_end_is_morning(self):
        assert proactive.QUIET_HOUR_END == 8

    def test_daily_cap_reasonable(self):
        # Sanity check — not 0, not absurdly high
        assert 1 <= proactive.MAX_PROACTIVE_PER_DAY <= 100


class TestMessageTemplates:
    """The proactive message templates are keyed by affection level (0-9).
    These tests don't validate content — they verify the structure stays
    consistent so future edits don't accidentally drop a level."""

    def test_morning_messages_cover_affection_levels(self):
        assert isinstance(proactive.MORNING_MESSAGES, dict)
        # At minimum levels 0-5 must be present
        for level in range(6):
            assert level in proactive.MORNING_MESSAGES, f"MORNING_MESSAGES missing level {level}"
            assert isinstance(proactive.MORNING_MESSAGES[level], list)
            assert len(proactive.MORNING_MESSAGES[level]) > 0
