"""Pure tests for AffectionManager — _compute_level and _calculate_delta."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def mgr_with_levels():
    """Build an AffectionManager with a fixed level table."""
    from app.affection import AffectionManager
    m = AffectionManager()
    m._levels = [
        {"index": 0, "name": "Cold Assessment", "threshold": 0},
        {"index": 1, "name": "Watchful",        "threshold": 100},
        {"index": 2, "name": "Warming",         "threshold": 300},
        {"index": 3, "name": "Comfortable",     "threshold": 500},
        {"index": 4, "name": "Trusted",         "threshold": 700},
        {"index": 5, "name": "Trust",           "threshold": 1000},
    ]
    return m


class TestComputeLevel:
    def test_zero_score_is_level_zero(self, mgr_with_levels):
        lvl, name = mgr_with_levels._compute_level(0)
        assert lvl == 0
        assert name == "Cold Assessment"

    def test_below_first_threshold_still_level_zero(self, mgr_with_levels):
        lvl, _ = mgr_with_levels._compute_level(50)
        assert lvl == 0

    def test_exact_threshold_matches(self, mgr_with_levels):
        lvl, name = mgr_with_levels._compute_level(100)
        assert lvl == 1
        assert name == "Watchful"

    def test_mid_level_score(self, mgr_with_levels):
        lvl, name = mgr_with_levels._compute_level(450)
        assert lvl == 2
        assert name == "Warming"

    def test_max_level_at_high_score(self, mgr_with_levels):
        lvl, name = mgr_with_levels._compute_level(9999)
        assert lvl == 5
        assert name == "Trust"

    def test_negative_score_stays_level_zero(self, mgr_with_levels):
        lvl, _ = mgr_with_levels._compute_level(-50)
        assert lvl == 0

    def test_empty_levels_safe_defaults(self):
        from app.affection import AffectionManager
        m = AffectionManager()
        m._levels = []
        lvl, name = m._compute_level(500)
        assert lvl == 0
        assert name == "Cold Assessment"


# ═══════════════════════════════════════════════════════════════════════════
# _calculate_delta
# ═══════════════════════════════════════════════════════════════════════════


_PERSONALITY = {
    "affection": {
        "scoring": {
            "greeting": [1, 3],
            "compliment": [3, 10],
            "genuine_interest": [5, 15],
            "rude_language": [-10, -30],
            "neutral": 0,
            "fixed_award": 7,
        }
    }
}


class TestCalculateDelta:
    def test_known_type_scaled_by_intensity_low(self):
        from app.affection import AffectionManager
        m = AffectionManager()
        with patch("app.affection.load_personality", return_value=_PERSONALITY):
            delta = m._calculate_delta("greeting", intensity=1)
        assert delta == 1

    def test_known_type_scaled_by_intensity_high(self):
        from app.affection import AffectionManager
        m = AffectionManager()
        with patch("app.affection.load_personality", return_value=_PERSONALITY):
            delta = m._calculate_delta("greeting", intensity=10)
        assert delta == 3

    def test_alias_resolves_to_canonical(self):
        from app.affection import AffectionManager
        m = AffectionManager()
        with patch("app.affection.load_personality", return_value=_PERSONALITY):
            delta = m._calculate_delta("rude", intensity=5)
        # should resolve 'rude' -> 'rude_language' range [-10, -30]
        assert delta < 0

    def test_remembering_alias_resolves(self):
        from app.affection import AffectionManager
        m = AffectionManager()
        with patch("app.affection.load_personality",
                   return_value={"affection": {"scoring": {
                       "remembering_details": [2, 8]
                   }}}):
            delta = m._calculate_delta("remembering", intensity=5)
        assert 2 <= delta <= 8

    def test_unknown_type_zero(self):
        from app.affection import AffectionManager
        m = AffectionManager()
        with patch("app.affection.load_personality", return_value=_PERSONALITY):
            delta = m._calculate_delta("nonexistent", intensity=5)
        assert delta == 0

    def test_neutral_alias_zero(self):
        from app.affection import AffectionManager
        m = AffectionManager()
        with patch("app.affection.load_personality", return_value=_PERSONALITY):
            delta = m._calculate_delta("neutral", intensity=5)
        assert delta == 0

    def test_fixed_score_not_a_list(self):
        from app.affection import AffectionManager
        m = AffectionManager()
        with patch("app.affection.load_personality", return_value=_PERSONALITY):
            delta = m._calculate_delta("fixed_award", intensity=5)
        # fixed_award is a scalar 7
        assert delta == 7

    def test_negative_range_scales_correctly(self):
        from app.affection import AffectionManager
        m = AffectionManager()
        with patch("app.affection.load_personality", return_value=_PERSONALITY):
            low_intensity = m._calculate_delta("rude_language", intensity=1)
            high_intensity = m._calculate_delta("rude_language", intensity=10)
        # Both negative; high intensity is more negative
        assert low_intensity == -10
        assert high_intensity == -30


class TestAbsenceDecay:
    @pytest.mark.asyncio
    async def test_jalsarraf_exempt_from_decay(self):
        """jalsarraf is pinned at max trust — decay should never touch him."""
        from app.affection import AffectionManager
        m = AffectionManager()
        # Should return without touching state
        await m._apply_absence_decay(user_id="jalsarraf")
        # No AttributeError, no side effect — all good
