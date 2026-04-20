"""Pure-function coverage for memory_archive.py — categories + quality scoring."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ═══════════════════════════════════════════════════════════════════════════
# available_categories — level-gated
# ═══════════════════════════════════════════════════════════════════════════


class TestAvailableCategories:
    def test_level_zero_returns_base_only(self):
        from app.memory_archive import available_categories, CATEGORIES_BY_LEVEL
        cats = available_categories(0)
        # Level 0 should include categories with min_level == 0
        base = CATEGORIES_BY_LEVEL.get(0, [])
        for c in base:
            assert c in cats

    def test_level_9_includes_all_lower_levels(self):
        from app.memory_archive import available_categories, CATEGORIES_BY_LEVEL
        cats = available_categories(9)
        for min_level, level_cats in CATEGORIES_BY_LEVEL.items():
            if min_level <= 9:
                for c in level_cats:
                    assert c in cats

    def test_level_5_excludes_higher_gated(self):
        from app.memory_archive import available_categories, CATEGORIES_BY_LEVEL
        cats = available_categories(5)
        for min_level, level_cats in CATEGORIES_BY_LEVEL.items():
            if min_level > 5:
                for c in level_cats:
                    assert c not in cats

    def test_results_non_empty_at_level_zero(self):
        from app.memory_archive import available_categories
        # Even at the lowest level there should be SOME category for curation
        assert len(available_categories(0)) > 0


# ═══════════════════════════════════════════════════════════════════════════
# annotation_quality_score — heuristic scoring
# ═══════════════════════════════════════════════════════════════════════════


class TestAnnotationQualityScore:
    def test_empty_returns_zero(self):
        from app.memory_archive import annotation_quality_score
        assert annotation_quality_score("") == 0.0

    def test_leaked_cot_instant_zero(self):
        from app.memory_archive import annotation_quality_score
        for prefix in ("We need to", "The user is", "Let me think"):
            text = prefix + " generate something specific and detailed here"
            assert annotation_quality_score(text) == 0.0, f"failed for: {prefix}"

    def test_sentence_length_marker_in_annotation_zero(self):
        """Models sometimes echo their instruction — '1-2 sentence' is a tell."""
        from app.memory_archive import annotation_quality_score
        assert annotation_quality_score(
            "Write a 1-2 sentence entry about this specific detailed moment."
        ) == 0.0

    def test_clamped_to_range(self):
        from app.memory_archive import annotation_quality_score
        score = annotation_quality_score(
            "He brushed the scar on her shoulder in the morning office, rifle by the bed, "
            "coffee cooling while rain hit the rooftop."
        )
        assert 0.0 <= score <= 1.0

    def test_whisper_opener_penalized(self):
        from app.memory_archive import annotation_quality_score
        with_whisper = annotation_quality_score(
            "Whisper of something generic about their afternoon together in the park."
        )
        without = annotation_quality_score(
            "Something generic about their afternoon together in the park."
        )
        assert with_whisper < without

    def test_too_short_penalized(self):
        from app.memory_archive import annotation_quality_score
        short = annotation_quality_score("Short text.")
        longer = annotation_quality_score(
            "A reasonably detailed annotation with sensory grounding and specifics."
        )
        assert short < longer

    def test_too_long_penalized(self):
        from app.memory_archive import annotation_quality_score
        medium = annotation_quality_score(
            "Morning rain, coffee on the desk, rifle by the bed — small ritual."
        )
        verbose = annotation_quality_score(
            "Morning rain hit the window and I watched it while drinking coffee. "
            "The rifle was by the bed from last night's briefing, and the scar on "
            "my shoulder itched where it was healing, and the rooftop garden was "
            "starting to flower, and the neon lights from the street reflected in "
            "the puddles, and the café across the street opened early today, and "
            "I wondered if he'd been there yet, wondering whether to bring coffee "
            "upstairs or let him sleep in like last Sunday morning."
        )
        assert verbose < medium  # over 350 chars penalized

    def test_specific_markers_do_not_reduce_score(self):
        """Specific markers add a small bonus (capped at 1.0)."""
        from app.memory_archive import annotation_quality_score
        specific = annotation_quality_score(
            "His hand on my shoulder in the office at 0300, rifle on the desk, "
            "coffee going cold while the rain started."
        )
        neutral_baseline = annotation_quality_score(
            "A moment that was somewhat meaningful but largely nondescript."
        )
        # Specific >= baseline (specifics only add, don't subtract)
        assert specific >= neutral_baseline

    def test_generic_romantic_vocab_penalized(self):
        from app.memory_archive import annotation_quality_score
        generic_heavy = annotation_quality_score(
            "Souls entwined in the glow of dawn on moonlit sheets, "
            "hearts beat as one in this sanctuary."
        )
        clean = annotation_quality_score(
            "He kissed her shoulder in the office while coffee cooled on the desk."
        )
        assert generic_heavy < clean

    def test_score_never_exceeds_one(self):
        from app.memory_archive import annotation_quality_score
        # All positive markers
        loaded = annotation_quality_score(
            "Morning office briefing. Coffee on the desk, rifle by the bed, "
            "rain on the collar of his jacket. Her hand on his shoulder at 0300 "
            "before the rooftop run. The scar from the last op."
        )
        assert loaded <= 1.0
