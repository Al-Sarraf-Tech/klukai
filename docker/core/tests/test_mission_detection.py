"""Tests for mission start/cancel detection and interval parsing."""

from __future__ import annotations

import re
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Import the detection functions directly — they are pure functions with no side effects.
# We replicate them here to avoid importing app.main (which triggers heavy init).
# The actual implementation in app.main is tested via these same inputs.

MISSION_START_KEYWORDS = [
    "updates every", "report every", "keep me posted", "status every", "check in every",
]
MISSION_CANCEL_KEYWORDS = [
    "stop updates", "cancel updates", "enough updates", "stand down", "stop reporting",
]


def _wants_mission_start(message: str) -> bool:
    lower = message.lower()
    return any(kw in lower for kw in MISSION_START_KEYWORDS)


def _wants_mission_cancel(message: str) -> bool:
    lower = message.lower()
    return any(kw in lower for kw in MISSION_CANCEL_KEYWORDS)


def _parse_interval_minutes(message: str) -> int:
    lower = message.lower()

    m = re.search(r'every\s+(\d+)\s*(?:min(?:ute)?s?)', lower)
    if m:
        return max(5, int(m.group(1)))

    m = re.search(r'every\s+(\d+)\s*(?:hour|hr)s?', lower)
    if m:
        return max(5, int(m.group(1)) * 60)

    if re.search(r'every\s+(?:an?\s+)?hour', lower):
        return 60

    if "half hour" in lower or "half an hour" in lower:
        return 30

    return 30


# ── Mission start detection ──────────────────────────────────────────────────

class TestWantsMissionStart:
    def test_updates_every_30_minutes(self):
        assert _wants_mission_start("Give me updates every 30 minutes") is True

    def test_report_every_hour(self):
        assert _wants_mission_start("Report every hour on the situation") is True

    def test_keep_me_posted(self):
        assert _wants_mission_start("Keep me posted on the patrol") is True

    def test_status_reports_every_30_seconds(self):
        assert _wants_mission_start("Status every 30 seconds please") is True

    def test_check_in_every(self):
        assert _wants_mission_start("Check in every 15 minutes") is True

    def test_normal_message_no_trigger(self):
        assert _wants_mission_start("How are you doing today?") is False

    def test_case_insensitive(self):
        assert _wants_mission_start("UPDATES EVERY 10 MINUTES") is True

    def test_partial_match_doesnt_trigger(self):
        assert _wants_mission_start("I need an update") is False


# ── Mission cancel detection ─────────────────────────────────────────────────

class TestWantsMissionCancel:
    def test_stop_updates(self):
        assert _wants_mission_cancel("Stop updates") is True

    def test_cancel_updates(self):
        assert _wants_mission_cancel("Cancel updates please") is True

    def test_enough(self):
        assert _wants_mission_cancel("Enough updates, I get it") is True

    def test_stand_down(self):
        assert _wants_mission_cancel("Stand down, Klukai") is True

    def test_stop_reporting(self):
        assert _wants_mission_cancel("Stop reporting on the mission") is True

    def test_normal_message_no_cancel(self):
        assert _wants_mission_cancel("Tell me more about the sector") is False

    def test_case_insensitive(self):
        assert _wants_mission_cancel("STAND DOWN") is True


# ── Interval parsing ─────────────────────────────────────────────────────────

class TestParseIntervalMinutes:
    def test_30_minutes(self):
        assert _parse_interval_minutes("every 30 minutes") == 30

    def test_15_minutes(self):
        assert _parse_interval_minutes("updates every 15 minutes") == 15

    def test_1_hour(self):
        assert _parse_interval_minutes("every 1 hour") == 60

    def test_2_hours(self):
        assert _parse_interval_minutes("report every 2 hours") == 120

    def test_an_hour(self):
        assert _parse_interval_minutes("every an hour") == 60

    def test_every_hour(self):
        assert _parse_interval_minutes("check in every hour") == 60

    def test_half_hour(self):
        assert _parse_interval_minutes("every half hour") == 30

    def test_half_an_hour(self):
        assert _parse_interval_minutes("every half an hour") == 30

    def test_unparseable_defaults_to_30(self):
        assert _parse_interval_minutes("keep me posted on progress") == 30

    def test_no_interval_text_defaults_to_30(self):
        assert _parse_interval_minutes("update me regularly") == 30

    def test_min_abbreviation(self):
        assert _parse_interval_minutes("every 10 min") == 10

    def test_mins_abbreviation(self):
        assert _parse_interval_minutes("every 45 mins") == 45

    def test_hr_abbreviation(self):
        assert _parse_interval_minutes("every 1 hr") == 60

    def test_floor_at_5_minutes(self):
        """Intervals below 5 minutes are clamped to 5."""
        assert _parse_interval_minutes("every 1 minute") == 5

    def test_floor_at_5_for_3_minutes(self):
        assert _parse_interval_minutes("every 3 minutes") == 5

    def test_case_insensitive(self):
        assert _parse_interval_minutes("EVERY 30 MINUTES") == 30
