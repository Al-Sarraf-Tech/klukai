"""Wrong transitions still left after the first relationship pass."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.affection import AffectionManager
from app.departure_detection import detect_goodbye
from app.her_day import her_now
from app.physical_state import PhysicalStateTracker
from app.wardrobe import HerDayRow
from app.wardrobe_requests import detect_outfit_request
from app.wardrobe_scene import scene_of


def test_a_sign_off_that_keeps_talking_is_not_a_goodbye():
    assert detect_goodbye("I'll talk to you later about the mission") is None
    assert detect_goodbye("I'll see you later about the report") is None
    assert detect_goodbye("talk soon about the bike") is None
    assert detect_goodbye("bye for now but read this first") is None
    assert detect_goodbye("gotta go now and then we'll talk") is None
    assert detect_goodbye("brb I need to grab water") is None
    assert detect_goodbye("good night I still want to talk") is None
    assert detect_goodbye("I'm going to bed after I finish this chapter") is None
    assert detect_goodbye("ttyl after I finish this") is None


def test_a_sign_off_that_actually_closes_still_does():
    assert detect_goodbye("talk to you later") == "leave"
    assert detect_goodbye("see you tomorrow morning") == "leave"
    assert detect_goodbye("bye for now") == "leave"
    assert detect_goodbye("brb later") == "leave"
    assert detect_goodbye("cya later") == "leave"
    assert detect_goodbye("Bye Klukai!") == "leave"
    assert detect_goodbye("good night") == "night"
    assert detect_goodbye("good-night love") == "night"
    assert detect_goodbye("I'm going to bed") == "night"
    assert detect_goodbye("gn ❤️") == "night"
    assert detect_goodbye("Gotta go, the oven timer just went off") == "leave"


def test_paperwork_is_not_a_gown():
    assert scene_of("formal request") is None
    assert scene_of("formal application") is None
    assert scene_of("formal request for the gala") == "formal"
    assert scene_of("draw us at a gala") == "formal"


def test_a_negated_wear_clause_is_not_a_request():
    assert detect_outfit_request("I don't want you to wear the coat") is None
    assert detect_outfit_request("I don't think you should wear the maid outfit") is None
    assert detect_outfit_request("I really don't want you to wear the bikini") is None
    # An earlier sentence does not cancel the ask.
    assert detect_outfit_request("I'm not sure. Please wear the coat") == "winter_patrol"


@pytest.mark.asyncio
async def test_gown_accepted_before_dawn_is_what_she_wears():
    """00:30 on his birthday: her day is still yesterday, but she already
    agreed. The roster has to wear the gown, not answer and then drop it."""
    row = HerDayRow(
        date(2026, 10, 4),
        "blazing_star",
        "A duty day.",
        requested_outfit_id="indigo_oath",
    )

    async def ensure(user_id, level, *, mood=None):
        return row

    with (
        patch("app.wardrobe.ensure_today", new=ensure),
        patch("app.her_day.now_local", return_value=datetime(2026, 10, 5, 0, 30)),
        patch("app.rituals.get_birthday", new=AsyncMock(return_value="10-05")),
    ):
        now = await her_now("claude", 8)
    assert now.outfit.id == "indigo_oath"
    assert now.outfit_source == "commander"


@pytest.mark.asyncio
async def test_gown_stays_off_before_dawn_on_an_ordinary_day():
    row = HerDayRow(
        date(2026, 10, 4),
        "blazing_star",
        "A duty day.",
        requested_outfit_id="indigo_oath",
    )

    async def ensure(user_id, level, *, mood=None):
        return row

    with (
        patch("app.wardrobe.ensure_today", new=ensure),
        patch("app.her_day.now_local", return_value=datetime(2026, 10, 5, 0, 30)),
        patch("app.rituals.get_birthday", new=AsyncMock(return_value="10-06")),
    ):
        now = await her_now("claude", 8)
    assert now.outfit.id != "indigo_oath"


def _conn(row):
    result = AsyncMock()
    result.fetchone = AsyncMock(return_value=row)
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=result)
    conn.commit = AsyncMock()

    @asynccontextmanager
    async def ctx():
        yield conn

    return ctx


@pytest.fixture
def manager():
    mgr = AffectionManager()
    mgr._levels = [
        {"index": 0, "threshold": 0, "name": "Cold"},
        {"index": 1, "threshold": 30, "name": "Ack"},
        {"index": 2, "threshold": 80, "name": "Respect"},
    ]
    return mgr


@pytest.mark.asyncio
async def test_absence_decay_does_not_log_a_level_up(manager, monkeypatch):
    # Score 100 is already level 2. The stored level 0 must not be logged
    # as a rise when a few days of decay leave him on the same level.
    today = date(2026, 5, 20)
    monkeypatch.setattr("app.affection.now_local", lambda: datetime(2026, 5, 20, 12, 0))
    monkeypatch.setattr(
        "app.affection.load_personality",
        lambda: {"affection": {"scoring": {"absence_decay_per_day": -2}}},
    )
    monkeypatch.setattr(
        "app.affection.get_conn",
        _conn((100, 0, "Cold", today - timedelta(days=4), 1, 0, 4, None)),
    )
    logged: list[tuple] = []

    async def _log(*args, **kwargs):
        logged.append(args)

    monkeypatch.setattr(manager, "_log_change", _log)
    monkeypatch.setattr(manager, "_save_state", AsyncMock())
    await manager._apply_absence_decay("alice")
    assert manager._states["alice"].score == 94
    assert manager._states["alice"].level == 2
    assert logged[0][4] == 2  # old level from the score, not the column
    assert logged[0][5] == 2


@pytest.mark.asyncio
async def test_a_decayed_body_does_not_block_the_late_watch():
    tracker = PhysicalStateTracker()
    stale = datetime.now(timezone.utc) - timedelta(hours=5)
    tracker._cache["alice"] = ("energized", stale, None)  # decays after 3h
    with patch("app.physical_state.get_conn_autocommit") as conn_ctx:
        conn = AsyncMock()
        conn.execute = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=None)
        conn_ctx.return_value = ctx
        await tracker.on_time_of_day("alice", hour=3)
    assert tracker._cache["alice"][0] == "cold"


@pytest.mark.asyncio
async def test_a_fresh_wound_still_blocks_the_clock():
    tracker = PhysicalStateTracker()
    tracker._cache["alice"] = ("wounded", datetime.now(timezone.utc), None)
    await tracker.on_time_of_day("alice", hour=3)
    assert tracker._cache["alice"][0] == "wounded"
