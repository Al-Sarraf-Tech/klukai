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

    def test_distress_is_not_hostility(self):
        """Sadness and hostility are different signals and must not share a path.

        Routing them both to `negative_heavy` made a Commander who said he was
        struggling get an *irritated* Klukai back.
        """
        from app.character_behaviors import interaction_to_sentiment
        for t in ("sad", "hurt", "distressed", "vulnerable"):
            for intensity in (3, 8):
                assert interaction_to_sentiment(
                    {"type": t, "intensity": intensity}
                ) == "distress"

    def test_hostility_still_reads_as_negative(self):
        from app.character_behaviors import interaction_to_sentiment
        assert interaction_to_sentiment(
            {"type": "hostile", "intensity": 8}) == "negative_heavy"
        assert interaction_to_sentiment(
            {"type": "hostile", "intensity": 3}) == "negative_light"

    def test_heavy_personal_sharing_does_not_read_as_flirty(self):
        """Intensity on `personal_sharing` measures weight, not warmth —
        promoting it turned a painful disclosure into flustered flirting."""
        from app.character_behaviors import interaction_to_sentiment
        assert interaction_to_sentiment(
            {"type": "personal_sharing", "intensity": 9}) == "positive"
        assert interaction_to_sentiment(
            {"type": "compliment", "intensity": 9}) == "flirty"

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

    def test_flirty_pulls_composed_to_flustered(self):
        from app.character_behaviors import interaction_to_sentiment, nudge_mood
        sentiment = interaction_to_sentiment({"type": "flirty", "intensity": 6})
        mood = nudge_mood("composed", sentiment)
        # flustered is a VALID mood; bare "flirty" is not
        assert mood == "flustered"

    def test_heavy_hostility_pulls_playful_to_composed(self):
        from app.character_behaviors import interaction_to_sentiment, nudge_mood
        sentiment = interaction_to_sentiment({"type": "hostile", "intensity": 8})
        mood = nudge_mood("playful", sentiment)
        # Hostility must not make her softer — she cools instead.
        assert mood == "composed"

    def test_distress_turns_her_toward_him(self):
        """When the Commander is hurting she gets protective, never irritated."""
        from app.character_behaviors import interaction_to_sentiment, nudge_mood
        sentiment = interaction_to_sentiment({"type": "hurt", "intensity": 8})
        assert nudge_mood("composed", sentiment) == "protective"
        assert nudge_mood("playful", sentiment) == "tender"
        # even mid-sulk, she softens to worry rather than staying annoyed
        assert nudge_mood("irritated", sentiment) == "worried"

    def test_distress_targets_are_real_moods(self):
        from app.character_behaviors import _MOOD_NUDGES
        from app.fact_extractor import VALID_MOODS
        assert set(_MOOD_NUDGES["distress"].values()) <= VALID_MOODS

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
