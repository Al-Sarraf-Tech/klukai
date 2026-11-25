"""Relationship transitions: affection, wardrobe, weather, goodbyes, crown."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.affection import AffectionManager
from app.affection_types import compute_level
from app.departure_detection import detect_goodbye
from app.her_day import Block, effective_outfit
from app.wardrobe import HerDayRow, _row_from_db
from app.wardrobe_catalog import catalog, pick_daily_outfit
from app.wardrobe_scene import dress_for_scene, scene_of
from app.weather_mood import weather_phrase, weather_to_mood


def test_level_up_follows_the_highest_threshold_not_the_last_row():
    levels = [
        {"index": 2, "threshold": 80, "name": "High"},
        {"index": 1, "threshold": 30, "name": "Low"},
    ]
    assert compute_level(levels, 100) == (2, "High")
    assert compute_level(levels, 30) == (1, "Low")
    assert compute_level(levels, 29) == (0, "Cold Assessment")


def test_equal_thresholds_still_keep_the_later_row():
    levels = [
        {"index": 1, "threshold": 10, "name": "First"},
        {"index": 4, "threshold": 10, "name": "Second"},
    ]
    assert compute_level(levels, 10) == (4, "Second")


@pytest.fixture
def manager():
    mgr = AffectionManager()
    mgr._levels = [
        {"index": 0, "threshold": 0, "name": "Cold"},
        {"index": 1, "threshold": 30, "name": "Ack"},
        {"index": 2, "threshold": 80, "name": "Respect"},
    ]
    return mgr


def _conn(row):
    result = AsyncMock()
    result.fetchone = AsyncMock(return_value=row)
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=result)
    conn.commit = AsyncMock()

    @asynccontextmanager
    async def ctx():
        yield conn

    return ctx, conn


@pytest.mark.asyncio
async def test_stale_level_column_does_not_skip_a_level_up(manager, monkeypatch):
    today = date(2026, 5, 20)
    now = datetime(2026, 5, 20, 12, 0, 0)
    monkeypatch.setattr("app.affection.now_local", lambda: now)
    monkeypatch.setattr(
        "app.affection.load_personality",
        lambda: {"affection": {"scoring": {"greeting": 1, "daily_points_cap": 8}}},
    )
    # Score 100 is already level 2. The stored level 0 must not announce 0→2.
    ctx, conn = _conn((100, 0, "Cold", today, 1, 0, 4, now))
    monkeypatch.setattr("app.affection.get_conn", ctx)
    monkeypatch.setattr("app.affection.get_conn_autocommit", ctx)
    change = await manager.apply_classification("greeting", 1, "alice")
    assert change.delta == 1
    assert change.new_score == 101
    assert change.new_level == 2
    assert change.level_changed is False
    assert conn.commit.await_count >= 1


@pytest.mark.asyncio
async def test_failed_turn_and_her_line_do_not_move_affection(manager, monkeypatch):
    today = date(2026, 5, 20)
    now = datetime(2026, 5, 20, 12, 0, 0)
    monkeypatch.setattr("app.affection.now_local", lambda: now)
    ctx, conn = _conn((100, 2, "Respect", today, 1, 0, 4, now))
    monkeypatch.setattr("app.affection.get_conn", ctx)
    monkeypatch.setattr("app.affection.get_conn_autocommit", ctx)
    failed = await manager.classify_and_adjust("you're amazing", "", "alice")
    hers = await manager.classify_and_adjust("", "I missed you.", "alice")
    assert failed.delta == 0 and failed.reason == "no_turn"
    assert hers.delta == 0 and hers.new_score == 100
    updates = [
        call.args[0]
        for call in conn.execute.await_args_list
        if call.args and str(call.args[0]).lstrip().upper().startswith("UPDATE")
    ]
    assert updates == []


@pytest.mark.parametrize(
    "msg",
    [
        "I have to go to the store later",
        "I have to go to the store, later",
        "have to go to the store later",
    ],
)
def test_a_later_errand_is_not_a_goodbye(msg):
    assert detect_goodbye(msg) is None


def test_a_real_sign_off_still_closes():
    assert detect_goodbye("later") == "leave"
    assert detect_goodbye("Gotta go, later") == "leave"
    assert detect_goodbye("I have to go to work") == "leave"


def test_scene_words_do_not_dress_her_for_a_different_scene():
    cat = catalog()
    assert scene_of("pool of applicants") is None
    assert scene_of("shore up the plan") is None
    assert scene_of("what's the date") is None
    assert scene_of("the range of options") is None
    assert scene_of("commission report") is None
    assert scene_of("formal complaint") is None
    assert scene_of("formal complaint at the gala") == "formal"
    assert scene_of("engine of the conversation") is None
    assert scene_of("by the pool") == "swim"
    assert scene_of("our movie date") == "date"
    assert scene_of("draw us at a gala") == "formal"
    assert (
        dress_for_scene("show me you at the gym", 9, current="blazing_star", cat=cat)
        == "range_kit"
    )


def test_denied_kit_and_the_gown_below_its_level_are_not_worn():
    cat = catalog()
    day = date(2026, 10, 5)
    base = HerDayRow(day, "blazing_star", "A duty day.")
    late = Block(
        "late", 0, 5, "klukadile_hours", "Quarters", "awake",
        private=True, outfit="klukadile_pajamas",
    )
    assert effective_outfit(base, late, 9, cat) == ("blazing_star", "auto")
    gown = HerDayRow(day, "blazing_star", "x", requested_outfit_id="indigo_oath")
    assert effective_outfit(gown, None, 8, cat)[0] != "indigo_oath"
    assert effective_outfit(gown, None, 7, cat) == ("blazing_star", "auto")
    assert effective_outfit(
        gown, None, 8, cat, occasions=frozenset({"white_day"})
    ) == ("indigo_oath", "commander")
    assert effective_outfit(gown, None, 9, cat) == ("indigo_oath", "commander")
    assert effective_outfit(
        HerDayRow(day, "starlit_vow", "x"), None, 9, cat
    ) == ("indigo_oath", "auto")


def test_bad_weather_does_not_throw_the_day_or_replace_the_mood():
    row = _row_from_db(
        date(2026, 10, 4),
        ("speed_star", "Off-duty plans.", None, 0, "{not json"),
    )
    assert row.base_outfit_id == "speed_star"
    assert row.weather is None
    oid, _reason = pick_daily_outfit(
        catalog(), date(2026, 7, 4), 9, weather={"temp_c": {"bad": True}, "condition": 3}
    )
    assert oid != "indigo_oath"
    assert weather_to_mood(None, 5) is None
    assert weather_to_mood({}, 5) is None
    assert weather_to_mood({"condition": ""}, 5) is None
    assert weather_to_mood("clear", 5) is None  # type: ignore[arg-type]
    assert weather_phrase({}) == ""
    assert weather_to_mood({"condition": "storm"}, 5) == "protective"


@pytest.mark.asyncio
async def test_crowning_one_tribute_does_not_write_two_jewels():
    from app import tributes

    result = AsyncMock()
    result.fetchone = AsyncMock(return_value=("real-id",))
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=result)
    conn.commit = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.connection = MagicMock(return_value=ctx)
    with patch("app.tributes.get_pool", return_value=pool):
        assert await tributes.set_crown_jewel("alice", "real-id") is True
    updates = [
        call.args[0]
        for call in conn.execute.await_args_list
        if "UPDATE" in str(call.args[0])
    ]
    assert len(updates) == 1
    assert "is_crown_jewel = (id = %s)" in updates[0]
    assert "pg_advisory_xact_lock" in conn.execute.await_args_list[0].args[0]
