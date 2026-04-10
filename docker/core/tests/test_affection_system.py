"""Tests for the affection scoring system: levels, deltas, caps, decay logic."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("psycopg")

from app.affection import AffectionManager, AffectionState


class TestAffectionState:
    """AffectionState model validation."""

    def test_default_state(self):
        state = AffectionState()
        assert state.score == 0
        assert state.level == 0
        assert state.level_name == "Cold Assessment"
        assert state.daily_points_earned == 0

    def test_custom_state(self):
        state = AffectionState(score=500, level=5, level_name="Trusted Ally")
        assert state.score == 500
        assert state.level == 5

    def test_max_score_boundary(self):
        state = AffectionState(score=1000, level=9, level_name="Devoted Oath")
        assert state.score == 1000


class TestLevelComputation:
    """Level mapping from score to level index/name."""

    @pytest.fixture
    def manager(self):
        mgr = AffectionManager()
        mgr._levels = [
            {"index": 0, "threshold": 0, "name": "Cold Assessment"},
            {"index": 1, "threshold": 20, "name": "Professional Respect"},
            {"index": 2, "threshold": 50, "name": "Trusted Ally"},
            {"index": 3, "threshold": 80, "name": "Guarded Care"},
            {"index": 4, "threshold": 120, "name": "Conflicted Warmth"},
            {"index": 5, "threshold": 180, "name": "Admitted Bond"},
            {"index": 6, "threshold": 280, "name": "Protective Fire"},
            {"index": 7, "threshold": 420, "name": "Unveiled Heart"},
            {"index": 8, "threshold": 600, "name": "Bonded"},
            {"index": 9, "threshold": 820, "name": "Devoted Oath"},
            {"index": 10, "threshold": 1000, "name": "Oath Eternal"},
        ]
        return mgr

    def test_zero_score_is_cold(self, manager):
        level, name = manager._compute_level(0)
        assert level == 0
        assert name == "Cold Assessment"

    def test_threshold_boundary_exact(self, manager):
        level, name = manager._compute_level(20)
        assert level == 1
        assert name == "Professional Respect"

    def test_between_thresholds(self, manager):
        level, name = manager._compute_level(35)
        assert level == 1  # Still "Professional Respect"

    def test_max_score(self, manager):
        level, name = manager._compute_level(1000)
        assert level == 10
        assert name == "Oath Eternal"

    def test_over_max(self, manager):
        level, name = manager._compute_level(1500)
        assert level == 10  # Capped at max level

    def test_level_progression_monotonic(self, manager):
        """Levels must strictly increase with score."""
        prev_level = -1
        for score in range(0, 1001, 10):
            level, _ = manager._compute_level(score)
            assert level >= prev_level
            prev_level = level


class TestDeltaMapping:
    """Verify affection point awards for each interaction type."""

    @pytest.fixture
    def manager(self, personality_config_path, monkeypatch):
        # _calculate_delta calls load_personality() with no path — point it at the repo file.
        monkeypatch.setenv("PERSONALITY_PATH", personality_config_path)
        # Clear cached personality so the env var takes effect.
        import app.personality as _p
        _p._PERSONALITY = None
        _p._PERSONALITY_PATH = ""
        mgr = AffectionManager()
        mgr._levels = [
            {"index": 0, "threshold": 0, "name": "Cold Assessment"},
        ]
        mgr._state = AffectionState(score=100, level=1, level_name="Professional Respect")
        return mgr

    def test_greeting_delta(self, manager):
        """Greetings earn 0-1 points."""
        delta = manager._calculate_delta("greeting", 3)
        assert 0 <= delta <= 1

    def test_compliment_delta(self, manager):
        """Compliments earn more points at higher intensity."""
        low = manager._calculate_delta("compliment", 3)
        high = manager._calculate_delta("compliment", 8)
        assert high >= low

    def test_rude_delta_negative(self, manager):
        """Rude interactions cause score loss."""
        delta = manager._calculate_delta("rude", 7)
        assert delta < 0

    def test_neutral_delta_zero(self, manager):
        """Neutral interactions earn 0 points."""
        delta = manager._calculate_delta("neutral", 1)
        assert delta == 0

    def test_genuine_interest_positive(self, manager):
        """Genuine interest always earns positive points."""
        delta = manager._calculate_delta("genuine_interest", 5)
        assert delta > 0
