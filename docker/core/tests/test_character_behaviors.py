"""Tests for app/character_behaviors.py."""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─────────────────────────────────────────────────────────────────────────
# classify_return_greeting
# ─────────────────────────────────────────────────────────────────────────


class TestClassifyReturnGreeting:
    def test_still_active_under_min(self):
        from app.character_behaviors import classify_return_greeting
        assert classify_return_greeting(hours_away=2, local_hour=10) == "silent"

    def test_too_stale_over_max(self):
        from app.character_behaviors import classify_return_greeting
        assert classify_return_greeting(hours_away=100, local_hour=10) == "silent"

    def test_long_overnight_morning_returns_dream(self):
        from app.character_behaviors import classify_return_greeting
        # 12h away, arriving at 8am local
        assert classify_return_greeting(hours_away=12, local_hour=8) == "dream"

    def test_mid_day_reconnect_returns_reflection(self):
        from app.character_behaviors import classify_return_greeting
        # 12h away but 2pm -> not morning window -> reflection
        assert classify_return_greeting(hours_away=12, local_hour=14) == "reflection"

    def test_short_absence_regardless_of_hour_returns_reflection(self):
        from app.character_behaviors import classify_return_greeting
        # 9h away at 8am — short of 10h dream threshold
        assert classify_return_greeting(hours_away=9, local_hour=8) == "reflection"


# ─────────────────────────────────────────────────────────────────────────
# is_anniversary + select_anniversary_from_firsts
# ─────────────────────────────────────────────────────────────────────────


class TestIsAnniversary:
    def test_exact_match_one_year_ago(self):
        from app.character_behaviors import is_anniversary
        now = datetime(2026, 4, 20, tzinfo=timezone.utc)
        past = datetime(2025, 4, 20, tzinfo=timezone.utc)
        result = is_anniversary(past, today=now)
        assert result is not None
        assert result["years"] == 1

    def test_exact_match_five_years_ago(self):
        from app.character_behaviors import is_anniversary
        now = datetime(2026, 4, 20, tzinfo=timezone.utc)
        past = datetime(2021, 4, 20, tzinfo=timezone.utc)
        assert is_anniversary(past, today=now)["years"] == 5

    def test_same_year_returns_none(self):
        """An event from earlier this year is not an anniversary."""
        from app.character_behaviors import is_anniversary
        now = datetime(2026, 4, 20, tzinfo=timezone.utc)
        earlier = datetime(2026, 1, 15, tzinfo=timezone.utc)
        assert is_anniversary(earlier, today=now) is None

    def test_different_day_returns_none(self):
        from app.character_behaviors import is_anniversary
        now = datetime(2026, 4, 20, tzinfo=timezone.utc)
        past = datetime(2025, 4, 19, tzinfo=timezone.utc)
        assert is_anniversary(past, today=now) is None

    def test_naive_dates_accepted(self):
        from app.character_behaviors import is_anniversary
        now = datetime(2026, 4, 20)  # naive
        past = datetime(2025, 4, 20)
        assert is_anniversary(past, today=now) is not None

    def test_missing_event_date_returns_none(self):
        from app.character_behaviors import is_anniversary
        assert is_anniversary(None) is None

    def test_accepts_plain_date(self):
        """companion_firsts.event_date is a SQL DATE -> psycopg returns date.

        Regression: `date` has no `.tzinfo`, which used to raise AttributeError
        and take down the whole daily anniversary sweep.
        """
        from app.character_behaviors import is_anniversary
        now = datetime(2026, 4, 20, tzinfo=timezone.utc)
        result = is_anniversary(date(2025, 4, 20), today=now)
        assert result is not None
        assert result["years"] == 1

    def test_accepts_plain_date_for_today(self):
        from app.character_behaviors import is_anniversary
        result = is_anniversary(date(2025, 4, 20), today=date(2026, 4, 20))
        assert result is not None
        assert result["years"] == 1

    def test_plain_date_non_match_returns_none(self):
        from app.character_behaviors import is_anniversary
        now = datetime(2026, 4, 20, tzinfo=timezone.utc)
        assert is_anniversary(date(2025, 4, 19), today=now) is None

    def test_accepts_iso_date_string(self):
        from app.character_behaviors import is_anniversary
        now = datetime(2026, 4, 20, tzinfo=timezone.utc)
        assert is_anniversary("2025-04-20", today=now)["years"] == 1

    def test_malformed_string_returns_none(self):
        from app.character_behaviors import is_anniversary
        now = datetime(2026, 4, 20, tzinfo=timezone.utc)
        assert is_anniversary("not-a-date", today=now) is None


class TestSelectAnniversaryFromFirsts:
    def test_picks_anniversary_match(self):
        from app.character_behaviors import select_anniversary_from_firsts
        today = datetime(2026, 4, 20, tzinfo=timezone.utc)
        firsts = [
            {"event_type": "first_message",
             "event_date": datetime(2025, 4, 20, tzinfo=timezone.utc)},
            {"event_type": "first_laugh",
             "event_date": datetime(2025, 7, 3, tzinfo=timezone.utc)},
        ]
        pick = select_anniversary_from_firsts(firsts, today=today)
        assert pick is not None
        assert pick["event_type"] == "first_message"
        assert pick["years"] == 1

    def test_picks_oldest_when_multiple_match(self):
        from app.character_behaviors import select_anniversary_from_firsts
        today = datetime(2026, 4, 20, tzinfo=timezone.utc)
        firsts = [
            {"event_type": "first_message",
             "event_date": datetime(2025, 4, 20, tzinfo=timezone.utc)},
            {"event_type": "first_kiss",
             "event_date": datetime(2023, 4, 20, tzinfo=timezone.utc)},
        ]
        pick = select_anniversary_from_firsts(firsts, today=today)
        assert pick["years"] == 3  # older event wins

    def test_accepts_iso_string_dates(self):
        from app.character_behaviors import select_anniversary_from_firsts
        today = datetime(2026, 4, 20, tzinfo=timezone.utc)
        firsts = [{"event_type": "first_message",
                   "event_date": "2025-04-20T00:00:00+00:00"}]
        assert select_anniversary_from_firsts(firsts, today=today) is not None

    def test_malformed_date_skipped(self):
        from app.character_behaviors import select_anniversary_from_firsts
        today = datetime(2026, 4, 20, tzinfo=timezone.utc)
        firsts = [
            {"event_type": "bad", "event_date": "not-a-date"},
            {"event_type": "good",
             "event_date": datetime(2025, 4, 20, tzinfo=timezone.utc)},
        ]
        pick = select_anniversary_from_firsts(firsts, today=today)
        assert pick["event_type"] == "good"

    def test_no_matches_returns_none(self):
        from app.character_behaviors import select_anniversary_from_firsts
        today = datetime(2026, 4, 20, tzinfo=timezone.utc)
        firsts = [
            {"event_type": "first_message",
             "event_date": datetime(2025, 6, 11, tzinfo=timezone.utc)},
        ]
        assert select_anniversary_from_firsts(firsts, today=today) is None

    def test_accepts_plain_date_rows(self):
        """Rows straight out of psycopg carry `date`, not `datetime`.

        Regression for the crash that killed the daily anniversary sweep.
        """
        from app.character_behaviors import select_anniversary_from_firsts
        today = datetime(2026, 4, 20, tzinfo=timezone.utc)
        firsts = [
            {"event_type": "first_message", "event_date": date(2025, 4, 20)},
            {"event_type": "first_laugh", "event_date": date(2025, 7, 3)},
        ]
        pick = select_anniversary_from_firsts(firsts, today=today)
        assert pick is not None
        assert pick["event_type"] == "first_message"
        assert pick["years"] == 1

    def test_mixed_date_and_datetime_rows(self):
        from app.character_behaviors import select_anniversary_from_firsts
        today = datetime(2026, 4, 20, tzinfo=timezone.utc)
        firsts = [
            {"event_type": "first_message", "event_date": date(2025, 4, 20)},
            {"event_type": "first_kiss",
             "event_date": datetime(2023, 4, 20, tzinfo=timezone.utc)},
        ]
        assert select_anniversary_from_firsts(firsts, today=today)["years"] == 3


# ─────────────────────────────────────────────────────────────────────────
# nudge_mood
# ─────────────────────────────────────────────────────────────────────────


class TestNudgeMood:
    def test_heavy_negative_pulls_playful_to_composed(self):
        from app.character_behaviors import nudge_mood
        # Hostility must not make her softer (old tender mapping was wrong).
        assert nudge_mood("playful", "negative_heavy") == "composed"

    def test_heavy_negative_composed_goes_irritated(self):
        from app.character_behaviors import nudge_mood
        assert nudge_mood("composed", "negative_heavy") == "irritated"

    def test_light_negative_pulls_playful_to_composed(self):
        from app.character_behaviors import nudge_mood
        assert nudge_mood("playful", "negative_light") == "composed"

    def test_positive_sentiment_warms_composed_mood(self):
        from app.character_behaviors import nudge_mood
        assert nudge_mood("composed", "positive") == "quietly_pleased"

    def test_flirty_sentiment_pulls_composed_to_flustered(self):
        from app.character_behaviors import nudge_mood
        # flirty is not a VALID mood name; flustered is.
        assert nudge_mood("composed", "flirty") == "flustered"

    def test_unknown_sentiment_keeps_mood(self):
        from app.character_behaviors import nudge_mood
        assert nudge_mood("composed", "totally-unknown") == "composed"

    def test_unknown_mood_keeps_mood(self):
        from app.character_behaviors import nudge_mood
        # No mapping for "berserk" in negative_heavy table
        assert nudge_mood("berserk", "negative_heavy") == "berserk"


# ─────────────────────────────────────────────────────────────────────────
# fade_score + should_fade
# ─────────────────────────────────────────────────────────────────────────


class TestFadeScore:
    def test_fresh_episode_keeps_full_score(self):
        from app.character_behaviors import fade_score
        assert fade_score(importance=10, age_days=0) == 10.0

    def test_half_life_halves_score(self):
        from app.character_behaviors import fade_score
        # importance 10, age = half_life -> ~5
        assert abs(fade_score(importance=10, age_days=30) - 5.0) < 0.01

    def test_two_half_lives_quarter(self):
        from app.character_behaviors import fade_score
        assert abs(fade_score(importance=10, age_days=60) - 2.5) < 0.01

    def test_negative_age_treated_as_zero(self):
        from app.character_behaviors import fade_score
        assert fade_score(importance=10, age_days=-5) == 0.0

    def test_zero_importance_stays_zero(self):
        from app.character_behaviors import fade_score
        assert fade_score(importance=0, age_days=30) == 0.0


class TestShouldFade:
    def test_high_importance_recent_not_fade(self):
        from app.character_behaviors import should_fade
        assert should_fade(importance=10, age_days=5) is False

    def test_low_importance_old_fades(self):
        from app.character_behaviors import should_fade
        # importance 2, 90 days, half_life 30 -> 2 * 0.125 = 0.25 < 0.5 threshold
        assert should_fade(importance=2, age_days=90) is True

    def test_threshold_configurable(self):
        from app.character_behaviors import should_fade
        # With threshold 3, importance 10 at 30 days -> effective 5 > 3 -> don't fade
        assert should_fade(importance=10, age_days=30, threshold=3.0) is False
        # With threshold 6, effective 5 < 6 -> fade
        assert should_fade(importance=10, age_days=30, threshold=6.0) is True
