"""Module-level proactive state and shared constants.

Holds the guardrail constants, the local-time helper, the major-mission-event
list, and the module-level active-mission-timer singleton used by the
idle-unload check.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .mission import MissionTimer


# The Commander's timezone. The container runs UTC, so every wall-clock
# decision (cron hours, quiet hours, time-of-day context) must go through
# this zone — zoneinfo handles DST, unlike a hand-converted CST offset.
LOCAL_TZ = ZoneInfo("America/Chicago")


def now_local() -> datetime:
    """Current wall-clock time in the Commander's timezone (naive).

    Single source of truth for "what time is it for him?" across the
    proactive engine, mood context, and reflection helpers. Returns a naive
    datetime so it composes with the engine's existing naive timestamps
    (mute expiry, event cooldowns). Patch this in tests to freeze the clock.
    """
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)


# Guardrails
MAX_PROACTIVE_PER_DAY = 23
QUIET_HOUR_START = 23  # 2300 hours America/Chicago
QUIET_HOUR_END = 8     # 0800 hours America/Chicago

# ── Module-level mission state (for idle-unload check) ────────────────────
# Forward-declared; MissionTimer class is defined in mission.py.
_active_mission_timer: MissionTimer | None = None


def has_active_mission() -> bool:
    """Return True if a mission timer is currently active (used by idle unload)."""
    return _active_mission_timer is not None and _active_mission_timer.active


# ── Major mission events ──────────────────────────────────────────────────
MAJOR_EVENTS = [
    "ambush",
    "squad_injured",
    "klukai_injured",
    "equipment_failure",
    "weather",
    "comms_disruption",
    "discovery",
    "medical_emergency",
    "mechty_asleep",       # Mechty fell asleep at a critical moment
    "belka_reckless",      # Belka charged ahead without orders
    "andoris_freeze",      # Andoris processing lag under fire
]
