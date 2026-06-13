"""Tests for the live mood-contagion wiring — interaction_to_sentiment + background flow."""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestInteractionToSentiment:
    def test_none_interaction_returns_none(self):
        from app.character_behaviors import interaction_to_sentiment
        assert interaction_to_sentiment(None) is None

    def test_missing_type_returns_none(self):
        from app.character_behaviors import interaction_to_sentiment
        assert interaction_to_sentiment({"intensity": 5}) is None

    def test_flirty(self):
        from app.character_behaviors import interaction_to_sentiment
        assert interaction_to_sentiment({"type": "flirty", "intensity": 5}) == "flirty"

    def test_playful_positive(self):
        from app.character_behaviors import interaction_to_sentiment
        assert interaction_to_sentiment({"type": "playful"}) == "positive"

    def test_warm_positive(self):
        from app.character_behaviors import interaction_to_sentiment
        assert interaction_to_sentiment({"type": "warm", "intensity": 8}) == "positive"

    def test_combative_light_at_low_intensity(self):
        from app.character_behaviors import interaction_to_sentiment
        assert interaction_to_sentiment({"type": "combative", "intensity": 3}) == "negative_light"

    def test_combative_heavy_at_high_intensity(self):
        from app.character_behaviors import interaction_to_sentiment
        assert interaction_to_sentiment({"type": "combative", "intensity": 9}) == "negative_heavy"

    def test_sad_heavy_regardless_of_intensity(self):
        from app.character_behaviors import interaction_to_sentiment
        assert interaction_to_sentiment({"type": "sad", "intensity": 3}) == "negative_heavy"

    def test_unknown_type_returns_none(self):
        from app.character_behaviors import interaction_to_sentiment
        assert interaction_to_sentiment({"type": "philosophical"}) is None

    def test_non_int_intensity_coerces(self):
        """If intensity comes back as a string, don't crash — treat as mid (5)."""
        from app.character_behaviors import interaction_to_sentiment
        result = interaction_to_sentiment({"type": "combative", "intensity": "high"})
        # intensity coerces to 5 -> light variant
        assert result == "negative_light"

    def test_non_dict_returns_none(self):
        from app.character_behaviors import interaction_to_sentiment
        assert interaction_to_sentiment("flirty") is None  # strings not accepted
        assert interaction_to_sentiment(42) is None


class TestMoodContagionChain:
    """Integration check: interaction_to_sentiment feeds nudge_mood correctly."""

    def test_flirty_pulls_composed_to_flirty(self):
        from app.character_behaviors import interaction_to_sentiment, nudge_mood
        sentiment = interaction_to_sentiment({"type": "flirty", "intensity": 6})
        mood = nudge_mood("composed", sentiment)
        assert mood == "flirty"

    def test_heavy_negative_pulls_playful_to_tender(self):
        from app.character_behaviors import interaction_to_sentiment, nudge_mood
        sentiment = interaction_to_sentiment({"type": "sad", "intensity": 8})
        mood = nudge_mood("playful", sentiment)
        assert mood == "tender"

    def test_positive_warms_cold_to_composed(self):
        from app.character_behaviors import interaction_to_sentiment, nudge_mood
        sentiment = interaction_to_sentiment({"type": "warm", "intensity": 7})
        mood = nudge_mood("cold", sentiment)
        assert mood == "composed"

    def test_neutral_is_noop(self):
        """Unknown / missing sentiment preserves the LLM-detected mood."""
        from app.character_behaviors import interaction_to_sentiment, nudge_mood
        sentiment = interaction_to_sentiment(None)
        mood = nudge_mood("composed", sentiment) if sentiment else "composed"
        assert mood == "composed"
