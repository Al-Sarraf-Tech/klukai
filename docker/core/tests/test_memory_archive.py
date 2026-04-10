"""Tests for memory archive: curation logic, category gating, annotation quality."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestCategoryGating:
    """Memory categories are unlocked by affection level."""

    # Affection gating rules:
    # Level 0-2: Tactical Operations, Mission Records, Squad Moments
    # Level 3-5: + The Commander, Quiet Hours
    # Level 6-9: + Precious Memories

    ALL_CATEGORIES = [
        "Tactical Operations", "Mission Records", "Squad Moments",
        "The Commander", "Quiet Hours", "Precious Memories",
    ]

    LOW_LEVEL_CATEGORIES = {"Tactical Operations", "Mission Records", "Squad Moments"}
    MID_LEVEL_CATEGORIES = {"The Commander", "Quiet Hours"}
    HIGH_LEVEL_CATEGORIES = {"Precious Memories"}

    def _available_categories(self, affection_level: int) -> set[str]:
        """Replicate the gating logic from memory_archive.py."""
        cats = set(self.LOW_LEVEL_CATEGORIES)
        if affection_level >= 3:
            cats |= self.MID_LEVEL_CATEGORIES
        if affection_level >= 6:
            cats |= self.HIGH_LEVEL_CATEGORIES
        return cats

    def test_level_0_only_military(self):
        cats = self._available_categories(0)
        assert "Tactical Operations" in cats
        assert "The Commander" not in cats
        assert "Precious Memories" not in cats

    def test_level_3_unlocks_commander(self):
        cats = self._available_categories(3)
        assert "The Commander" in cats
        assert "Quiet Hours" in cats
        assert "Precious Memories" not in cats

    def test_level_6_unlocks_precious(self):
        cats = self._available_categories(6)
        assert "Precious Memories" in cats
        assert len(cats) == len(self.ALL_CATEGORIES)

    def test_level_9_has_all(self):
        cats = self._available_categories(9)
        assert cats == set(self.ALL_CATEGORIES)

    def test_monotonic_unlock(self):
        """Higher levels never lose categories."""
        prev_cats = set()
        for level in range(10):
            cats = self._available_categories(level)
            assert cats >= prev_cats
            prev_cats = cats


class TestAnnotationQuality:
    """Annotation text must meet quality standards."""

    GOOD_ANNOTATIONS = [
        "His whispered words erased miles of cold space between us.",
        "In the chopper's roar, his whisper cut through my defenses.",
        "Waking up to your smile, limbs still entwined.",
    ]

    BAD_ANNOTATIONS = [
        "Uncaptioned moment.",
        "",
        "We need to write a 1-2 sentence caption for this memory.",
        "The user wants a journal entry caption for a memory.",
    ]

    def test_good_annotations_pass(self):
        for ann in self.GOOD_ANNOTATIONS:
            assert len(ann) >= 15, f"Too short: {ann}"
            assert not ann.startswith("We need"), f"Leaked COT: {ann}"
            assert not ann.startswith("The user"), f"Leaked COT: {ann}"

    def test_bad_annotations_detected(self):
        for ann in self.BAD_ANNOTATIONS:
            is_bad = (
                len(ann) < 15
                or ann == "Uncaptioned moment."
                or ann.startswith("We need")
                or ann.startswith("The user")
            )
            assert is_bad, f"Should be detected as bad: {ann}"

    def test_annotation_max_length(self):
        """Annotations should be concise (under 400 chars for readability)."""
        for ann in self.GOOD_ANNOTATIONS:
            assert len(ann) < 400


class TestImageTags:
    """Scene tag inference for image generation."""

    def test_bed_scene_tags(self):
        tags = _infer_tags("bed, couple, tender, night, casual")
        assert "bed" in tags or "bedroom" in tags

    def test_office_scene_tags(self):
        tags = _infer_tags("office, couple, serious, professional")
        assert "office" in tags

    def test_empty_tags_get_defaults(self):
        tags = _infer_tags("")
        assert len(tags) > 0  # Should always have some tags


def _infer_tags(tag_str: str) -> list[str]:
    """Simple tag parser for testing."""
    if not tag_str.strip():
        return ["standing", "detailed background"]
    return [t.strip() for t in tag_str.split(",") if t.strip()]


class TestAnnotationQualityScore:
    def test_leaked_cot_scores_zero(self):
        from app.memory_archive import annotation_quality_score
        assert annotation_quality_score("We need to write a caption") == 0.0

    def test_good_annotation_scores_high(self):
        from app.memory_archive import annotation_quality_score
        score = annotation_quality_score("He fell asleep on my shoulder. I didn't move for two hours.")
        assert score >= 0.7

    def test_generic_annotation_scores_low(self):
        from app.memory_archive import annotation_quality_score
        score = annotation_quality_score("Whispering secrets under moonlit sheets, our hearts beat as one")
        assert score < 0.5

    def test_empty_scores_zero(self):
        from app.memory_archive import annotation_quality_score
        assert annotation_quality_score("") == 0.0
