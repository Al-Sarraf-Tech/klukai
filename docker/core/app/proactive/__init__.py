"""Proactive engagement: Klukai-themed scheduled check-ins and mission reports.

This package was split from a single ``proactive.py`` module. The public API is
re-exported here so existing imports (``from .proactive import ProactiveEngine``,
``from .proactive import has_active_mission``) continue to work unchanged.
"""

from __future__ import annotations

from .engine import ProactiveEngine
from .mission import MissionTimer
from .state import (
    MAJOR_EVENTS,
    MAX_PROACTIVE_PER_DAY,
    QUIET_HOUR_END,
    QUIET_HOUR_START,
    has_active_mission,
)
from .templates import (
    EVENING_MESSAGES,
    IDLE_MESSAGES,
    MISSION_REPORTS,
    MORNING_MESSAGES,
    ROMANCE_MESSAGES,
)

__all__ = [
    "ProactiveEngine",
    "MissionTimer",
    "has_active_mission",
    "MAJOR_EVENTS",
    "MAX_PROACTIVE_PER_DAY",
    "QUIET_HOUR_START",
    "QUIET_HOUR_END",
    "MORNING_MESSAGES",
    "EVENING_MESSAGES",
    "IDLE_MESSAGES",
    "MISSION_REPORTS",
    "ROMANCE_MESSAGES",
]
