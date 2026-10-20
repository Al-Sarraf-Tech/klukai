"""Her heart rate every turn: one base per mood, shaded by intensity."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import heartbeat
from app.heartbeat import (
    BASE_BPM,
    HIGH_INTENSITY_MOODS,
    MAX_BPM,
    MIN_BPM,
    coerce_intensity,
    heart_rate,
    spike_bpm,
)

NEW_MOODS = {
    "serene": 60,
    "cozy": 62,
    "wistful": 66,
    "lonely": 70,
    "stern": 70,
    "smug": 74,
    "hopeful": 76,
    "sulking": 78,
    "admiring": 80,
    "teasing": 82,
    "restless": 88,
    "heartbroken": 92,
    "giddy": 104,
    "overwhelmed": 118,
    "breathless": 140,
}

_DART = Path(__file__).resolve().parents[3] / "flutter_app/lib/theme/mood_visuals.dart"


def test_every_mood_has_a_base_rate_and_nothing_else_does():
    from app.models import Mood

    assert set(BASE_BPM) == {m.value for m in Mood}
    assert len(BASE_BPM) == 65


def test_new_moods_use_the_agreed_base_rates():
    for mood, bpm in NEW_MOODS.items():
        assert BASE_BPM[mood] == bpm, mood


def test_existing_rates_match_a_known_sample():
    # Spot values from flutter_app/lib/theme/mood_visuals.dart, the source of
    # truth for the 50 moods that existed before.
    assert BASE_BPM["composed"] == 65
    assert BASE_BPM["drowsy"] == 55
    assert BASE_BPM["panicked"] == 180
    assert BASE_BPM["protective"] == 105
    assert BASE_BPM["embarrassed"] == 96


@pytest.mark.skipif(not _DART.is_file(), reason="Flutter sources not checked out")
def test_rates_match_the_app_mood_visuals():
    """Any mood the app knows must beat at the same base rate in core."""
    entries = re.findall(
        r"'([a-z_]+)':\s*MoodVisual\(Color\(0x[0-9A-Fa-f]+\),\s*(\d+)\)",
        _DART.read_text(),
    )
    assert len(entries) >= 50
    for mood, bpm in entries:
        assert BASE_BPM[mood] == int(bpm), mood


@pytest.mark.parametrize(
    ("mood", "intensity", "expected"),
    [
        ("composed", 5, 65),
        ("composed", 10, 80),
        ("composed", 1, 53),
        ("drowsy", 1, 48),  # 55 - 12 = 43, clamped up
        ("panicked", 10, 190),  # 180 + 15 = 195, clamped down
        ("breathless", 7, 146),
        ("serene", 3, 54),
    ],
)
def test_heart_rate_formula_and_clamp(mood, intensity, expected):
    assert heart_rate(mood, intensity) == expected


def test_unknown_mood_uses_the_app_default():
    assert heart_rate("bogus", 5) == heartbeat.DEFAULT_BPM == 70


def test_heart_rate_never_leaves_bounds():
    for mood in BASE_BPM:
        for intensity in range(-5, 20):
            assert MIN_BPM <= heart_rate(mood, intensity) <= MAX_BPM


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (7, 7),
        (99, 10),
        (0, 1),
        (-3, 1),
        ("8", 8),
        (" 4 ", 4),
        (6.6, 6),
        ("high", 5),
        (None, 5),
        (True, 5),
        ([], 5),
        (float("nan"), 5),
        (float("inf"), 5),
    ],
)
def test_coerce_intensity(raw, expected):
    assert coerce_intensity(raw) == expected


def test_spike_moods_include_the_new_intense_ones():
    for mood in ("overwhelmed", "breathless", "heartbroken"):
        assert mood in HIGH_INTENSITY_MOODS
    for mood, bpm in HIGH_INTENSITY_MOODS.items():
        assert mood in BASE_BPM
        assert 150 <= bpm <= 200, mood


def test_spike_only_at_high_intensity():
    assert spike_bpm("passionate", 7) == 165
    assert spike_bpm("passionate", 6) is None
    assert spike_bpm("composed", 10) is None
    assert spike_bpm("breathless", 9) == HIGH_INTENSITY_MOODS["breathless"]
