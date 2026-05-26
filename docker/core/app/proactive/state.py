"""Module-level proactive state and shared constants.

Holds the guardrail constants, the major-mission-event list, and the
module-level active-mission-timer singleton used by the idle-unload check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .mission import MissionTimer


# Guardrails
MAX_PROACTIVE_PER_DAY = 23
QUIET_HOUR_START = 23  # 2300 hours
QUIET_HOUR_END = 8     # 0800 hours

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
