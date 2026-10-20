"""Her heart rate, every turn.

Each mood has a resting base rate; the turn's intensity (1-10) shades it by
3 bpm a step around the midpoint. The 50 moods that predate this module copy
their base rate from ``flutter_app/lib/theme/mood_visuals.dart``, the app's
single source of truth for mood visuals, so the number core sends and the
number the app would show for the same mood agree. The 15 newer moods use the
values agreed with the app in the same change.

``HIGH_INTENSITY_MOODS`` is the separate spike alert (``heartbeat_spike``):
only these moods, only at intensity 7 or more, and always 150 bpm or higher
so the app's red flash fires.
"""

from __future__ import annotations

import math

MIN_BPM = 48
MAX_BPM = 190
DEFAULT_BPM = 70  # kDefaultMoodVisual in mood_visuals.dart
DEFAULT_INTENSITY = 5
SPIKE_MIN_INTENSITY = 7

BASE_BPM: dict[str, int] = {
    # Core
    "composed": 65,
    "focused": 78,
    "prideful": 80,
    "exasperated": 90,
    "protective": 105,
    "quietly_pleased": 72,
    "competitive": 100,
    "tender": 75,
    "longing": 80,
    "battle_ready": 130,
    # Romantic
    "flustered": 95,
    "affectionate": 78,
    "shy": 88,
    "yearning": 85,
    "devoted": 82,
    "passionate": 115,
    "jealous": 108,
    "possessive": 112,
    "smitten": 92,
    "infatuated": 98,
    # Tactical
    "vigilant": 95,
    "calculating": 88,
    "hunting": 110,
    "adrenaline": 145,
    # Mission stress
    "scared": 140,
    "terrified": 165,
    "panicked": 180,
    "desperate": 175,
    "relieved": 62,
    # Relaxed
    "content": 60,
    "playful": 76,
    "drowsy": 55,
    "amused": 74,
    "bored": 58,
    "excited": 105,
    # Dark
    "melancholic": 68,
    "haunted": 78,
    "conflicted": 85,
    "guilty": 82,
    "determined": 95,
    "grieving": 72,
    "furious": 120,
    # Additional
    "nostalgic": 70,
    "curious": 72,
    "irritated": 92,
    "defiant": 98,
    "vulnerable": 88,
    "grateful": 73,
    "worried": 88,
    "embarrassed": 96,
    # Added 2026-10-19
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

# Spike alert BPM for the moods intense enough to flash the app red.
HIGH_INTENSITY_MOODS: dict[str, int] = {
    "passionate": 165,
    "terrified": 175,
    "panicked": 180,
    "desperate": 170,
    "furious": 160,
    "adrenaline": 155,
    "yearning": 150,
    "infatuated": 152,
    "heartbroken": 150,
    "overwhelmed": 158,
    "breathless": 162,
}


def coerce_intensity(value: object) -> int:
    """Clamp an extractor intensity into 1-10; anything unreadable is 5."""
    if isinstance(value, bool):
        return DEFAULT_INTENSITY
    number: float
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return DEFAULT_INTENSITY
    else:
        return DEFAULT_INTENSITY
    if not math.isfinite(number):
        return DEFAULT_INTENSITY
    return max(1, min(10, int(number)))


def heart_rate(mood: str, intensity: int) -> int:
    """clamp(base + (intensity - 5) * 3, 48, 190) for this mood."""
    base = BASE_BPM.get(mood, DEFAULT_BPM)
    step = coerce_intensity(intensity) - DEFAULT_INTENSITY
    return max(MIN_BPM, min(MAX_BPM, base + step * 3))


def spike_bpm(mood: str, intensity: int) -> int | None:
    """The spike alert's BPM, or None when this turn does not spike."""
    if mood in HIGH_INTENSITY_MOODS and intensity >= SPIKE_MIN_INTENSITY:
        return HIGH_INTENSITY_MOODS[mood]
    return None
