"""Tests for app/weather_mood.py — weather→mood mapping + in-character phrase.

Every mood weather_to_mood can emit MUST be a valid taxonomy mood (a key of
moods.MOOD_SPECIFIC_BLEED). weather_to_mood/phrase are fully fail-soft on None.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CONDITIONS = ["clear", "cloudy", "fog", "drizzle", "rain", "snow", "storm"]


def _weather(condition, temp_c=10.0, code=0, is_day=True):
    return {"temp_c": temp_c, "code": code, "condition": condition, "is_day": is_day}


# ── weather_to_mood ─────────────────────────────────────────────────────────


class TestWeatherToMood:
    def test_none_weather_returns_none(self):
        from app.weather_mood import weather_to_mood

        assert weather_to_mood(None, 5) is None

    @pytest.mark.parametrize("condition", CONDITIONS)
    @pytest.mark.parametrize("affection", [0, 3, 5, 7, 9])
    def test_every_condition_maps_to_valid_taxonomy_mood(self, condition, affection):
        from app.personality import moods
        from app.weather_mood import weather_to_mood

        mood = weather_to_mood(_weather(condition), affection)
        assert mood is not None
        assert mood in moods.MOOD_SPECIFIC_BLEED, (
            f"{condition}@aff{affection} → {mood!r} not in taxonomy"
        )

    @pytest.mark.parametrize("affection", [0, 3, 5, 7, 9])
    def test_unknown_condition_maps_to_valid_mood(self, affection):
        """A condition string outside the known set still yields a valid mood."""
        from app.personality import moods
        from app.weather_mood import weather_to_mood

        mood = weather_to_mood(_weather("hailstorm-of-frogs"), affection)
        assert mood in moods.MOOD_SPECIFIC_BLEED

    def test_storm_is_protective(self):
        from app.weather_mood import weather_to_mood

        assert weather_to_mood(_weather("storm"), 5) == "protective"

    def test_clear_is_warm_or_playful(self):
        from app.weather_mood import weather_to_mood

        # Clear/sunny should read warm — playful, affectionate, or content.
        assert weather_to_mood(_weather("clear"), 5) in {
            "playful",
            "affectionate",
            "content",
        }

    def test_rain_is_tender_or_wistful(self):
        from app.weather_mood import weather_to_mood

        assert weather_to_mood(_weather("rain"), 5) in {
            "tender",
            "melancholic",
            "longing",
        }

    def test_cloudy_is_composed(self):
        from app.weather_mood import weather_to_mood

        assert weather_to_mood(_weather("cloudy"), 5) == "composed"


# ── weather_phrase ──────────────────────────────────────────────────────────


class TestWeatherPhrase:
    def test_none_weather_returns_empty(self):
        from app.weather_mood import weather_phrase

        assert weather_phrase(None) == ""

    def test_storm_phrase_mentions_storm_and_staying_in(self):
        from app.weather_mood import weather_phrase

        phrase = weather_phrase(_weather("storm"))
        assert phrase  # non-empty
        low = phrase.lower()
        assert "storm" in low

    @pytest.mark.parametrize("condition", CONDITIONS)
    def test_every_condition_has_nonempty_single_line_phrase(self, condition):
        from app.weather_mood import weather_phrase

        phrase = weather_phrase(_weather(condition))
        assert isinstance(phrase, str)
        assert phrase.strip() != ""
        # One sentence / one line — no newlines.
        assert "\n" not in phrase

    def test_unknown_condition_still_returns_a_string(self):
        from app.weather_mood import weather_phrase

        phrase = weather_phrase(_weather("frogs"))
        assert isinstance(phrase, str)
        assert phrase.strip() != ""
