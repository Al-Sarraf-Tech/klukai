"""Physical state tracker: Klukai's body awareness — soreness, energy, comfort.

States decay over time back to 'normal'. No tick loop needed — we check
elapsed time when the state is queried. Persists to PostgreSQL so it
survives restarts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .db import get_conn, get_conn_autocommit

logger = logging.getLogger(__name__)

# ── Physical states and their natural decay times ───────────────────────
STATES = {
    "normal":    {"decay_hours": None, "description": ""},
    "sore":      {"decay_hours": 4,    "description": "muscles ache from recent combat"},
    "exhausted": {"decay_hours": 6,    "description": "drained after prolonged deployment"},
    "cold":      {"decay_hours": 2,    "description": "chilled — late watch, no warmth nearby"},
    "warm":      {"decay_hours": 2,    "description": "comfortable warmth — close to the Commander or by a heat source"},
    "relaxed":   {"decay_hours": 3,    "description": "at ease after a long conversation or quiet time"},
    "wounded":   {"decay_hours": 8,    "description": "field-dressed injury still tender"},
    "energized": {"decay_hours": 3,    "description": "sharp and ready after rest or a good morning"},
}


def get_description(state: str) -> str:
    """Get the human-readable description for a physical state."""
    return STATES.get(state, STATES["normal"])["description"]


def should_decay(state: str, since: datetime) -> bool:
    """Check if a physical state has exceeded its natural decay time."""
    if state == "normal":
        return False
    info = STATES.get(state)
    if not info or info["decay_hours"] is None:
        return False
    # `since` is read from a TIMESTAMPTZ column (tz-aware UTC via psycopg).
    # Compare in UTC, and tolerate a naive `since` (treat it as UTC) so a
    # subtraction can never crash the chat path with "can't subtract
    # offset-naive and offset-aware datetimes".
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    elapsed = datetime.now(timezone.utc) - since
    return elapsed > timedelta(hours=info["decay_hours"])


class PhysicalStateTracker:
    """Tracks Klukai's physical state per user. Reads from / writes to PostgreSQL."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, datetime, str | None]] = {}

    async def get_state(self, user_id: str = "jalsarraf") -> tuple[str, str]:
        """Return (state, description). Auto-decays expired states.

        Returns:
            Tuple of (state_name, description_string).
            Description is empty string for 'normal'.
        """
        state, since, detail = await self._load(user_id)

        # Auto-decay if time exceeded
        if should_decay(state, since):
            logger.info("Physical state decayed: %s -> normal (was set %s)", state, since)
            await self.set_state(user_id, "normal")
            return "normal", ""

        desc = detail or get_description(state)
        return state, desc

    async def set_state(
        self, user_id: str, state: str, detail: str | None = None
    ) -> None:
        """Set a new physical state. Overwrites current state."""
        if state not in STATES:
            logger.warning("Unknown physical state '%s', defaulting to normal", state)
            state = "normal"

        # Aware UTC so the cached `since` matches the tz-aware value read back
        # from the TIMESTAMPTZ column (keeps should_decay's comparison consistent).
        now = datetime.now(timezone.utc)
        self._cache[user_id] = (state, now, detail)

        try:
            async with get_conn_autocommit() as conn:
                await conn.execute(
                    "INSERT INTO companion_persistent_state "
                    "(user_id, physical_state, physical_state_since, physical_detail, updated_at) "
                    "VALUES (%s, %s, %s, %s, NOW()) "
                    "ON CONFLICT (user_id) DO UPDATE SET "
                    "physical_state = %s, physical_state_since = %s, "
                    "physical_detail = %s, updated_at = NOW()",
                    (user_id, state, now, detail, state, now, detail),
                )
        except Exception as e:
            logger.warning("Failed to persist physical state for %s: %s", user_id, e)

        logger.info("Physical state set: %s -> %s for user %s", state, detail or "", user_id)

    async def on_mission_end(self, user_id: str, had_injury: bool = False) -> None:
        """Transition physical state after a mission ends."""
        if had_injury:
            await self.set_state(user_id, "wounded", "field-dressed wound from the last mission")
        else:
            await self.set_state(user_id, "sore", "muscles aching after the operation")

    async def on_time_of_day(self, user_id: str, hour: int) -> None:
        """Suggest a physical state based on time of day (called from proactive checks).

        Only sets state if current state is 'normal' — doesn't override
        mission-related states.
        """
        state, _, _ = await self._load(user_id)
        if state != "normal":
            return  # Don't override active states

        if 0 <= hour < 6:
            await self.set_state(user_id, "cold", "the late watch is cold without company")
        elif 6 <= hour < 9:
            await self.set_state(user_id, "energized", "fresh after rest, ready for the day")

    async def on_long_conversation(self, user_id: str) -> None:
        """Set relaxed state after a long conversation (10+ turns)."""
        state, _, _ = await self._load(user_id)
        if state in ("normal", "cold"):
            await self.set_state(user_id, "relaxed", "at ease — the conversation was good")

    async def _load(self, user_id: str) -> tuple[str, datetime, str | None]:  # pragma: no cover - integration (DB)
        """Load physical state from cache or DB."""
        if user_id in self._cache:
            return self._cache[user_id]

        try:
            async with get_conn() as conn:
                row = await (await conn.execute(
                    "SELECT physical_state, physical_state_since, physical_detail "
                    "FROM companion_persistent_state WHERE user_id = %s",
                    (user_id,),
                )).fetchone()
                if row and row[0]:
                    state = row[0]
                    since = row[1] or datetime.now()
                    detail = row[2]
                    self._cache[user_id] = (state, since, detail)
                    return state, since, detail
        except Exception as e:
            logger.warning("Failed to load physical state for %s: %s", user_id, e)

        default = ("normal", datetime.now(), None)
        self._cache[user_id] = default
        return default
