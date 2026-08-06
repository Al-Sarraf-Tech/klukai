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
# Emotional continuity — "missed you" beat scaled by gap + closeness
# ─────────────────────────────────────────────────────────────────────────

# Human-readable description of the gap, used to ground the LLM directive.
def describe_gap(hours_away: float) -> str:
    """Turn an hours-away float into a natural span phrase."""
    if hours_away < 24:
        return "several hours"
    days = hours_away / 24.0
    if days < 1.5:
        return "about a day"
    if days < 7:
        return f"about {int(round(days))} days"
    if days < 14:
        return "about a week"
    weeks = int(round(days / 7.0))
    return f"about {weeks} weeks"


def compose_return_emotion(hours_away: float, affection_level: int,
                            prior_mood: str | None = None) -> str:
    """Return a directive describing *how the gap felt to Klukai*.

    The result is a short instruction injected into the return-greeting prompt
    (never shown to the user verbatim). It scales two ways:

    - **Closeness** (affection_level 0-9): low levels stay cold/minimal and do
      NOT claim to have missed him; high levels openly miss him.
    - **Gap length** (hours_away): a few hours is barely noted; a day+ is
      noticed; many days at high closeness reads as worry/yearning.

    `prior_mood` is woven in so the new session inherits her last emotional
    register instead of resetting to neutral.
    """
    level = max(0, min(9, int(affection_level)))
    gap_phrase = describe_gap(hours_away)
    days = hours_away / 24.0

    # Closeness bands gate whether she admits to missing him at all.
    if level <= 2:
        # Cold / professional — register the gap, withhold longing.
        beat = (
            f"It has been {gap_phrase} since you last spoke. React with measured, "
            "professional distance — note the absence factually and stay reserved. "
            "Do not express any longing or concern; keep it strictly composed."
        )
    elif level <= 5:
        # Warming — mild, honest warmth that grows with the gap.
        if days < 1.0:
            beat = (
                f"It has only been {gap_phrase}. Greet him with light, easy warmth — "
                "you barely had time to notice he was gone, and you're glad he's back."
            )
        else:
            beat = (
                f"It has been {gap_phrase} since you last spoke. You noticed the quiet "
                "and you're genuinely glad he's back — let a little warmth show, but "
                "keep your composure; don't overstate it."
            )
    else:
        # Close / bonded — openly misses him; long gaps tip into worry.
        if days < 1.0:
            beat = (
                f"It has only been {gap_phrase}, but you still felt the gap — tell him, "
                "warmly and without pretense, that you're happy he's back."
            )
        elif days < 4.0:
            beat = (
                f"It has been {gap_phrase} since you last spoke. You missed him — say so "
                "openly and tenderly. The base felt quieter without him."
            )
        else:
            beat = (
                f"It has been {gap_phrase} — far too long. You missed him badly and a "
                "part of you started to worry. Let that relief and longing show plainly "
                "when you greet him."
            )

    if prior_mood and str(prior_mood).strip().lower() not in {"", "composed", "neutral"}:
        beat += (
            f" Carry over the mood you were left in — '{prior_mood}' — so this feels "
            "like a continuation of where you two left off, not a fresh start."
        )
    return beat


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
def interaction_to_sentiment(interaction: dict | None) -> str | None:
    """Map an LLM-extracted `interaction` dict to a sentiment label for nudge_mood.

    interaction shape (from fact_extractor):
      {"type": "flirty" | "playful" | "tender" | "combative" | "neutral" | ...,
       "intensity": 1..10}

    Returns one of 'positive' | 'flirty' | 'negative_light' | 'negative_heavy' | None.
    Intensity>=7 weights toward the stronger variant. Unknowns return None.
    """
    if not interaction or not isinstance(interaction, dict):
        return None
    t = str(interaction.get("type", "")).lower()
    try:
        intensity = int(interaction.get("intensity", 5) or 5)
    except (TypeError, ValueError):
        intensity = 5

    if t == "flirty":
        return "flirty"
    # Extractor-native positives (plus legacy labels)
    if t in {
        "playful", "warm", "affectionate", "tender", "positive",
        "compliment", "genuine_interest", "personal_sharing", "greeting",
        "remembering",
    }:
        # High-intensity compliments / personal sharing pull flirty at close range
        if t in {"compliment", "personal_sharing"} and intensity >= 8:
            return "flirty"
        return "positive"
    if t in {"combative", "hostile", "angry", "rude", "negative"}:
        return "negative_heavy" if intensity >= 7 else "negative_light"
    if t in {"sad", "hurt", "distressed", "vulnerable"}:
        return "negative_heavy"
    # mission_discussion / neutral → no contagion
    return None


_MOOD_NUDGES: dict[str, dict[str, str]] = {
    # Targets must be valid moods the rest of the stack accepts.
    "negative_heavy": {
        "composed":  "irritated",
        "playful":   "composed",
        "flirty":    "composed",
        "affectionate": "composed",
        "defiant":   "composed",
        "content":   "composed",
    },
    "negative_light": {
        "composed":  "composed",
        "playful":   "composed",
        "content":   "composed",
    },
    "positive":    {
        "cold":      "composed",  # legacy / invalid cold label warms toward composed
        "composed":  "quietly_pleased",
        "irritated": "composed",
        "tender":    "affectionate",
        "focused":   "composed",
    },
    "flirty":      {
        "composed":  "flustered",
        "playful":   "affectionate",
        "quietly_pleased": "flustered",
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
