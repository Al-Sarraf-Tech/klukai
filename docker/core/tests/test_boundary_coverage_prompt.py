"""Prompt presence uses the real clock module and personality policy."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.chat_prompt import ChatPromptBuilder


def presence_builder(hours):
    return ChatPromptBuilder(
        memory=None,
        affection=None,
        proactive=None,
        router=None,
        ws=None,
        assemble_prompt=lambda **kwargs: "SYSTEM",
        personality=lambda: {},
        wants_thread=lambda text: False,
        needs_image=lambda text: False,
        hours_away=AsyncMock(return_value=hours),
        warm_budget=0.1,
        turn_budget=0.1,
    )


async def test_real_presence_policy_registers_two_hour_return():
    builder = presence_builder(2.0)
    state = SimpleNamespace(level=3, last_interaction_date=None, consecutive_days=0)
    assert await builder.presence("SYSTEM", state, "alice") == (
        "SYSTEM\n\nPRESENCE (color your opening; do not recite these bullets):\n"
        "It has been about 2 hours since you last spoke. "
        "Register it lightly if natural — no lecture."
    )
    builder.hours_away.assert_awaited_once_with("alice")


@pytest.mark.parametrize(
    "last,now,expected",
    [
        (date(2026, 10, 14), datetime(2026, 10, 15, 12), "about a day"),
        (datetime(2026, 10, 15, 10), datetime(2026, 10, 15, 12), "about 2 hours"),
        (
            datetime(2026, 10, 15, 10, tzinfo=timezone.utc),
            datetime(2026, 10, 15, 12, tzinfo=timezone.utc),
            "about 2 hours",
        ),
        (datetime(2026, 10, 15, 13), datetime(2026, 10, 15, 12), None),
        (None, datetime(2026, 10, 15, 12), None),
    ],
)
async def test_absence_fallback_handles_date_aware_naive_future_and_missing_values(
    monkeypatch, last, now, expected
):
    from app.proactive import state

    monkeypatch.setattr(state, "now_local", lambda: now)
    builder = presence_builder(None)
    aff = SimpleNamespace(level=3, last_interaction_date=last, consecutive_days=0)
    result = await builder.presence("SYSTEM", aff, "alice")
    if expected is None:
        assert result == "SYSTEM"
    else:
        assert result.startswith("SYSTEM\n\nPRESENCE") and expected in result
    builder.hours_away.assert_awaited_once_with("alice")


async def test_precise_exchange_gap_overrides_stale_calendar_day(monkeypatch):
    from app.proactive import state

    monkeypatch.setattr(
        state, "now_local", lambda: pytest.fail("calendar fallback should not run")
    )
    builder = presence_builder(2.0)
    result = await builder.presence(
        "SYSTEM",
        SimpleNamespace(
            level=3, last_interaction_date=date(2020, 1, 1), consecutive_days=0
        ),
        "alice",
    )
    assert "about 2 hours" in result and "weeks" not in result


async def test_recall_failure_leaves_chat_usable_without_partial_memory_context():
    builder = presence_builder(None)
    builder.memory = SimpleNamespace(
        recall_for_prompt=AsyncMock(side_effect=RuntimeError("unavailable memory"))
    )
    assert await builder.recall("a sufficiently long request", False, 3, "alice") == (
        [],
        {},
        [],
    )
    builder.memory.recall_for_prompt.assert_awaited_once_with(
        "a sufficiently long request", user_id="alice", affection_level=3
    )
