"""Validated affection state and transition records, without service ownership."""

from __future__ import annotations
from datetime import date, datetime
from pydantic import BaseModel
from typing import Any


class AffectionState(BaseModel):
    score: int = 0
    level: int = 0
    level_name: str = "Cold Assessment"
    last_interaction_date: date | None = None
    consecutive_days: int = 0
    daily_points_earned: int = 0
    total_interactions: int = 0
    first_interaction: datetime | None = None


class AffectionChange(BaseModel):
    delta: int
    reason: str
    new_score: int
    new_level: int
    new_level_name: str
    level_changed: bool
    level_direction: str = ""  # "up" or "down" or ""


def compute_level(levels: list[dict[str, Any]], score: int) -> tuple[int, str]:
    """Map score to level index and name."""
    result_level = 0
    result_name = "Cold Assessment"
    for lv in levels:
        if score >= lv.get("threshold", 0):
            result_level = lv.get("index", 0)
            result_name = lv.get("name", "Unknown")
    return result_level, result_name
