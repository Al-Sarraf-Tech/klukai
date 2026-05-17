"""Tests for app.memory_archive pure helpers — available_categories,
annotation_quality_score, _row_to_dict.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.memory_archive import (
    annotation_quality_score,
    available_categories,
    _row_to_dict,
)


class TestAvailableCategories:
    def test_level_0_minimal_categories(self):
        cats = available_categories(0)
        # Should at least have some baseline categories
        assert isinstance(cats, list)
        assert len(cats) > 0

    def test_higher_level_has_more_categories(self):
        cats_0 = available_categories(0)
        cats_9 = available_categories(9)
        # Higher affection unlocks more categories
        assert len(cats_9) >= len(cats_0)

    def test_no_duplicates_at_any_level(self):
        cats = available_categories(9)
        # Within a single call there should be no duplicates from level overlap
        # (if there are, that's a config bug in CATEGORIES_BY_LEVEL)
        assert len(cats) == len(set(cats))


class TestAnnotationQualityScore:
    def test_empty_returns_zero(self):
        assert annotation_quality_score("") == 0.0

    def test_leaked_chain_of_thought_returns_zero(self):
        assert annotation_quality_score("We need to write...") == 0.0
        assert annotation_quality_score("The user wants...") == 0.0
        assert annotation_quality_score("Let me think about this...") == 0.0

    def test_meta_instruction_returns_zero(self):
        assert annotation_quality_score("Write a 1-2 sentence caption for this") == 0.0

    def test_whisper_opener_penalized(self):
        score = annotation_quality_score("Whispered softly to me in the rain")
        assert 0 < score < 1.0

    def test_generic_romantic_words_penalized(self):
        # "intertwined", "sanctuary" trigger generic-word penalty
        score = annotation_quality_score(
            "Our souls entwined beneath the moonlit sheets in the sanctuary of love."
        )
        assert score < 0.7

    def test_specific_details_bonus(self):
        # Concrete details boost the score
        text = "Briefing at 0300 hours in the office. He brought coffee — strong, dark, the way I like it. I almost laughed."
        score = annotation_quality_score(text)
        assert score > 0.8

    def test_too_short_penalized(self):
        # < 30 chars
        score = annotation_quality_score("A short note.")
        assert score < 1.0

    def test_too_long_penalized(self):
        long_text = "x" * 400
        score = annotation_quality_score(long_text)
        # Length penalty applied
        assert score < 1.0

    def test_score_clamped_0_to_1(self):
        # Any input must produce a score in [0.0, 1.0]
        for text in ["", "x", "Whisper Whisper Whisper", "x" * 1000, "Briefing at 0300"]:
            s = annotation_quality_score(text)
            assert 0.0 <= s <= 1.0

    def test_clean_caption_high_score(self):
        # Specific + reasonable length + no generic words = high score
        text = "He left his jacket on the chair before I could thank him. I put it on later. It smelled like him."
        score = annotation_quality_score(text)
        assert score >= 0.8


class TestRowToDict:
    def test_full_row_serializes(self):
        ts = datetime(2026, 5, 17, 0, 0, 0, tzinfo=timezone.utc)
        row = ("abc-id", "klukai_1.png", "She smiled.", "Quiet Moments", ["smile", "evening"], ts)
        d = _row_to_dict(row)
        assert d == {
            "id": "abc-id",
            "filename": "klukai_1.png",
            "annotation": "She smiled.",
            "category": "Quiet Moments",
            "scene_tags": ["smile", "evening"],
            "created_at": "2026-05-17T00:00:00+00:00",
        }

    def test_null_annotation_becomes_empty_string(self):
        ts = datetime(2026, 5, 17, tzinfo=timezone.utc)
        row = ("id", "f.png", None, "Cat", [], ts)
        d = _row_to_dict(row)
        assert d["annotation"] == ""

    def test_null_scene_tags_becomes_empty_list(self):
        ts = datetime(2026, 5, 17, tzinfo=timezone.utc)
        row = ("id", "f.png", "anno", "Cat", None, ts)
        d = _row_to_dict(row)
        assert d["scene_tags"] == []

    def test_null_created_at_becomes_none(self):
        row = ("id", "f.png", "anno", "Cat", [], None)
        d = _row_to_dict(row)
        assert d["created_at"] is None

    def test_id_coerced_to_string(self):
        ts = datetime(2026, 5, 17, tzinfo=timezone.utc)
        # UUID objects, ints, etc. should all stringify
        row = (123, "f.png", "x", "Cat", [], ts)
        d = _row_to_dict(row)
        assert d["id"] == "123"
        assert isinstance(d["id"], str)
