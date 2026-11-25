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
    """Map score to the highest threshold it has reached, not the last row.

    A later row with a lower threshold must not pull a level-up back down, or
    one gain would skip the level the score actually crossed.
    """
    result_level = 0
    result_name = "Cold Assessment"
    best: int | None = None
    for lv in levels:
        threshold = lv.get("threshold", 0)
        if score >= threshold and (best is None or threshold >= best):
            best = threshold
            result_level = lv.get("index", 0)
            result_name = lv.get("name", "Unknown")
    return result_level, result_name
