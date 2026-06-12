"""Activity-pattern detection for smarter, less robotic proactivity.

``PatternsMixin`` looks at the Commander's recent ``companion_messages`` and
builds a per-user day-of-week activity profile. From that it derives gentle
behavioural patterns — e.g. ``quiet_on_sunday`` — that the events layer can use
to time an in-character check-in ("You've gone quiet this weekend, Commander.").

Design notes:
- Read-only. No new table; reuses ``companion_messages`` (created_at, user_id).
- Cached ~1h per user, mirroring ``MilestonesMixin.check_anniversaries`` so a
  burst of messages doesn't hammer the DB.
- Resilient: any DB error returns ``{}`` and logs at debug — proactivity is a
  nicety, never a hard dependency.
"""

from __future__ import annotations

import logging
from datetime import datetime

from .base import _EngineBase
from .state import now_local

logger = logging.getLogger(__name__)

# Day-of-week index → name. PostgreSQL EXTRACT(DOW ...) is 0=Sunday..6=Saturday,
# which matches this list, so the heatmap key lines up with the DB directly.
_DOW_NAMES = (
    "sunday", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday",
)

# How many days of history to profile, and the cache TTL.
_PATTERN_WINDOW_DAYS = 30
_PATTERN_CACHE_TTL_SECONDS = 3600  # ~1 hour

# A day must be this much quieter than the user's average to count as a "quiet
# day", and we need at least this many observed weeks before trusting it.
_QUIET_RATIO = 0.5          # day avg <= 50% of overall daily average
_MIN_WEEKS_OBSERVED = 2     # need a couple of that-weekday occurrences


class PatternsMixin(_EngineBase):
    """Day-of-week activity profiling + quiet-day pattern detection."""

    async def detect_activity_patterns(
        self, user_id: str = "jalsarraf"
    ) -> dict[str, dict]:
        """Return detected behavioural patterns for ``user_id``.

        Shape::

            {
                "quiet_on_sunday": {
                    "type": "quiet_day",
                    "day": "sunday",
                    "dow": 0,
                    "confidence": 0.0..1.0,
                    "user_msgs": <int>,      # avg user msgs on that weekday
                    "overall_avg": <float>,  # avg user msgs across all weekdays
                },
                ...
            }

        Cached for ~1h per user. On any DB error returns ``{}`` (logged debug).
        """
        cache_key = f"patterns:{user_id}"
        now = now_local()
        if hasattr(self, "_pattern_cache") and cache_key in self._pattern_cache:
            cached_at, cached_result = self._pattern_cache[cache_key]
            if (now - cached_at).total_seconds() < _PATTERN_CACHE_TTL_SECONDS:
                return cached_result

        patterns = await self._compute_quiet_day_patterns(user_id)

        if not hasattr(self, "_pattern_cache"):
            self._pattern_cache: dict[str, tuple[datetime, dict[str, dict]]] = {}
        self._pattern_cache[cache_key] = (now, patterns)
        return patterns

    async def _compute_quiet_day_patterns(self, user_id: str) -> dict[str, dict]:
        """Query the last ~30 days of user messages and find low-activity days."""
        from ..db import get_conn

        # rows: (dow 0..6, msg_count, distinct_days_active_on_that_weekday)
        # NB: use make_interval(days => %s) — a "%s" inside a quoted INTERVAL
        # string literal is NOT a psycopg placeholder, so it can't be bound.
        try:
            async with get_conn() as conn:
                rows = await (await conn.execute(
                    "SELECT EXTRACT(DOW FROM created_at)::int AS dow, "
                    "       COUNT(*) AS msgs, "
                    "       COUNT(DISTINCT created_at::date) AS active_days "
                    "FROM companion_messages "
                    "WHERE user_id = %s "
                    "  AND role = 'user' "
                    "  AND created_at > NOW() - make_interval(days => %s) "
                    "GROUP BY dow",
                    (user_id, _PATTERN_WINDOW_DAYS),
                )).fetchall()
        except Exception as e:
            logger.debug("Activity pattern query failed for %s: %s", user_id, e)
            return {}

        if not rows:
            return {}

        # Per-weekday average messages *per occurrence of that weekday* in the
        # window. Using active_days as the denominator (min 1) keeps a single
        # very chatty Saturday from masking many silent ones.
        per_day_avg: dict[int, float] = {}
        per_day_count: dict[int, int] = {}
        for dow, msgs, active_days in rows:
            occurrences = max(int(active_days or 0), 1)
            per_day_avg[int(dow)] = float(msgs) / occurrences
            per_day_count[int(dow)] = int(msgs)

        # Overall daily average across the weekdays we actually saw activity on.
        if not per_day_avg:
            return {}
        overall_avg = sum(per_day_avg.values()) / len(per_day_avg)
        if overall_avg <= 0:
            return {}

        # A weekday is "quiet" if its average is well below the overall average.
        # Days with zero messages in the window are the strongest signal — but we
        # only trust them once ~30 days has given us at least a couple of that
        # weekday's occurrences (>= _MIN_WEEKS_OBSERVED implied by the window).
        weeks_in_window = _PATTERN_WINDOW_DAYS / 7.0
        patterns: dict[str, dict] = {}
        for dow in range(7):
            day_avg = per_day_avg.get(dow, 0.0)
            if weeks_in_window < _MIN_WEEKS_OBSERVED:
                continue
            if day_avg <= overall_avg * _QUIET_RATIO:
                # Confidence: how far below average, clamped to 0..1. A fully
                # silent weekday => 1.0; one at exactly the threshold => ~0.5.
                deficit = (overall_avg - day_avg) / overall_avg
                confidence = max(0.0, min(1.0, deficit))
                day = _DOW_NAMES[dow]
                patterns[f"quiet_on_{day}"] = {
                    "type": "quiet_day",
                    "day": day,
                    "dow": dow,
                    "confidence": round(confidence, 3),
                    "user_msgs": per_day_count.get(dow, 0),
                    "overall_avg": round(overall_avg, 3),
                }

        return patterns
