"""Character-level behavior helpers: dream narration, anniversary surfacing,
mood contagion, and memory drift/fade.

These modules produce small pieces of *decision* data — "should Klukai greet
with a dream?", "is today an anniversary?" — that the higher-level flows
(chat, proactive) combine with their own orchestration.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Dream narration vs reflection classifier
# ─────────────────────────────────────────────────────────────────────────

GreetingKind = Literal["dream", "reflection", "silent"]


def classify_return_greeting(
    hours_away: float,
    local_hour: int,
    min_hours: float = 8.0,
    max_hours: float = 72.0,
    morning_start: int = 6,
    morning_end: int = 11,
) -> GreetingKind:
    """Decide what kind of return-greeting fits the context.

    Rules (first match wins):
    - Away < min_hours -> silent (still active, don't greet)
    - Away > max_hours -> silent (too stale; let user set tone)
    - Long overnight absence (>=10h) landing in morning window -> 'dream'
      (she had a night, now she tells you about it)
    - Otherwise -> 'reflection' (daytime / short absence)
    """
    if hours_away < min_hours:
        return "silent"
    if hours_away > max_hours:
        return "silent"
    if hours_away >= 10 and morning_start <= local_hour <= morning_end:
        return "dream"
    return "reflection"


# ─────────────────────────────────────────────────────────────────────────
# Anniversary surfacing
# ─────────────────────────────────────────────────────────────────────────


def is_anniversary(event_date: datetime, today: datetime | None = None) -> dict | None:
    """Return anniversary metadata if today matches event_date's month/day.

    For a date that occurred N years ago, returns {"years": N, "original": ISO}.
    For shorter intervals it reports months (3m+) or "today" on same day.

    Returns None if no special-date match.
    """
    if not event_date:
        return None
    now = today or datetime.now(timezone.utc)

    # Align timezone for comparison
    if event_date.tzinfo is None:
        event_date = event_date.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # Same year, same day = original date (not really an anniversary)
    if event_date.year == now.year:
        return None

    if event_date.month == now.month and event_date.day == now.day:
        years = now.year - event_date.year
        return {
            "years": years,
            "months": 0,
            "original": event_date.isoformat(),
        }

    # Monthly anniversary (same day-of-month) — only surface at 3, 6, 9, 12+
    if event_date.day == now.day and event_date.year == now.year - 1:
        # More-than-a-year but not-same-date handled above; skip
        pass

    return None


def select_anniversary_from_firsts(firsts: list[dict],
                                    today: datetime | None = None) -> dict | None:
    """Scan a list of 'firsts' (companion_firsts rows) for anniversary matches.

    Returns the first match (or the most significant by years) or None.
    Each firsts dict should contain at least 'event_type' and 'event_date'.
    """
    now = today or datetime.now(timezone.utc)
    best: dict | None = None
    for f in firsts:
        ed = f.get("event_date")
        if not ed:
            continue
        if isinstance(ed, str):
            try:
                ed = datetime.fromisoformat(ed.replace("Z", "+00:00"))
            except Exception:
                continue
        anniversary = is_anniversary(ed, today=now)
        if anniversary:
            entry = {
                "event_type": f.get("event_type", "event"),
                **anniversary,
            }
            if best is None or anniversary["years"] > best["years"]:
                best = entry
    return best


# ─────────────────────────────────────────────────────────────────────────
# Mood contagion
# ─────────────────────────────────────────────────────────────────────────

# Sentiment -> mood pull. Small magnitudes so a single message doesn't
# flip her whole register — it nudges.
_MOOD_NUDGES: dict[str, dict[str, str]] = {
    "negative_heavy": {
        "composed":  "tender",
        "playful":   "tender",
        "flirty":    "tender",
        "defiant":   "composed",
        "cold":      "composed",
    },
    "negative_light": {
        "composed":  "composed",
        "playful":   "composed",
        "cold":      "composed",
    },
    "positive":    {
        "cold":      "composed",
        "composed":  "playful",
        "tender":    "playful",
    },
    "flirty":      {
        "composed":  "flirty",
        "playful":   "flirty",
    },
}


def nudge_mood(current: str, sentiment: str) -> str:
    """Return new mood pulled toward user's sentiment. Bounded; never jumps
    more than one register per call.

    sentiment examples: 'negative_heavy', 'negative_light', 'positive', 'flirty'.
    """
    table = _MOOD_NUDGES.get(sentiment, {})
    return table.get(current, current)


# ─────────────────────────────────────────────────────────────────────────
# Memory drift / fade
# ─────────────────────────────────────────────────────────────────────────


def fade_score(importance: int, age_days: float,
                half_life_days: float = 30.0) -> float:
    """Decay an episode's effective importance by age.

    Effective = importance * 0.5 ** (age / half_life)

    importance 10 / 30 days / half_life 30 -> 5
    importance 10 / 90 days / half_life 30 -> 1.25
    """
    if importance <= 0 or age_days < 0:
        return 0.0
    decay = 0.5 ** (age_days / half_life_days)
    return max(0.0, min(float(importance), importance * decay))


def should_fade(importance: int, age_days: float,
                threshold: float = 0.5, half_life_days: float = 30.0) -> bool:
    """Return True if this episode's effective importance has fallen below
    `threshold` — candidate for compaction / archival."""
    return fade_score(importance, age_days, half_life_days) < threshold
