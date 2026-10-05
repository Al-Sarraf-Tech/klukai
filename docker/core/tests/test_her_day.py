"""Her Day: a deterministic duty roster, and one shared answer to "where is
she and what does she have on" for the prompt, the PWA and image generation."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import her_day as hd
from app import wardrobe as w
from app.personality import load_personality

SUNDAY = date(2026, 10, 4)
MONDAY = date(2026, 10, 5)


@pytest.fixture(scope="module")
def cfg() -> dict:
    return load_personality()["her_day"]


@pytest.fixture(scope="module")
def cat() -> dict:
    return w.catalog()


def _row(base="blazing_star", requested=None, weather=None, day=SUNDAY):
    return w.HerDayRow(day, base, "A duty day.", requested, 0, weather)


class TestBuildDay:
    def test_one_block_per_slot_in_order(self, cfg):
        blocks = hd.build_day(cfg, MONDAY, 5, seed="claude")
        assert [b.slot for b in blocks] == ["late", "early", "morning", "midday", "afternoon", "evening", "night"]
        assert [b.start for b in blocks] == sorted(b.start for b in blocks)

    def test_deterministic_per_day_and_seed(self, cfg):
        a = hd.build_day(cfg, MONDAY, 5, seed="claude")
        assert a == hd.build_day(cfg, MONDAY, 5, seed="claude")
        days = {tuple(b.id for b in hd.build_day(cfg, date(2026, 10, d), 5, seed="claude")) for d in range(1, 29)}
        assert len(days) > 10  # her days actually vary

    def test_weekday_rules(self, cfg):
        for d in range(5, 10):  # Mon-Fri
            ids = {b.id for b in hd.build_day(cfg, date(2026, 10, d), 5, seed="s")}
            assert "garage_morning" not in ids and "toy_bike" not in ids

    def test_no_riding_in_the_rain(self, cfg):
        riding = {"dawn_ride", "patrol_ride", "night_ride_out"}
        for d in range(1, 29):
            ids = {b.id for b in hd.build_day(cfg, date(2026, 10, d), 9, weather={"condition": "rain"}, seed="s")}
            assert not ids & riding

    def test_min_level_gates_the_onesie_hours(self, cfg):
        for d in range(1, 29):
            ids = {b.id for b in hd.build_day(cfg, date(2026, 10, d), 6, seed="s")}
            assert "klukadile_hours" not in ids

    def test_empty_slot_is_skipped_and_defaults_apply(self):
        cfg = {"slots": {"a": [0, 12], "b": [12, 24]}, "activities": [{"id": "x", "slots": ["a"]}, "junk"]}
        blocks = hd.build_day(cfg, SUNDAY, 0)
        assert len(blocks) == 1
        b = blocks[0]
        assert (b.location, b.activity, b.duty, b.private, b.outfit) == ("The Elmo", "", False, False, None)
        assert hd.build_day({}, SUNDAY, 0) == []

    def test_current_block(self, cfg):
        blocks = hd.build_day(cfg, MONDAY, 5, seed="s")
        assert hd.current_block(blocks, 2).slot == "late"
        assert hd.current_block(blocks, 13).slot == "midday"
        assert hd.current_block(blocks, 23).slot == "night"
        assert hd.current_block([], 9) is None


class TestBlockLabel:
    def test_private_life_is_private_below_three(self):
        b = hd.Block("evening", 18, 22, "x", "Lounge", "off duty, pretending to read", private=True)
        assert b.label(2) == ("Off duty", "")
        assert b.label(3) == ("Lounge", "off duty, pretending to read")

    def test_duty_is_always_visible(self):
        b = hd.Block("morning", 8, 12, "x", "Command deck", "squad briefing", duty=True)
        assert b.label(0) == ("Command deck", "squad briefing")


class TestEffectiveOutfit:
    def test_his_granted_request_wins(self, cat):
        block = hd.Block("afternoon", 14, 18, "h", "Hangar", "x", outfit="hangar_coveralls")
        assert hd.effective_outfit(_row(requested="speed_star"), block, 5, cat) == ("speed_star", "commander")

    def test_block_kit_beats_her_everyday_pick(self, cat):
        block = hd.Block("afternoon", 14, 18, "h", "Hangar", "x", outfit="hangar_coveralls")
        assert hd.effective_outfit(_row(), block, 5, cat) == ("hangar_coveralls", "roster")

    def test_special_day_survives_the_roster(self, cat):
        block = hd.Block("afternoon", 14, 18, "h", "Hangar", "x", outfit="hangar_coveralls")
        assert hd.effective_outfit(_row(base="black_cat_ops"), block, 5, cat) == ("black_cat_ops", "auto")

    def test_locked_block_kit_falls_back_to_base(self, cat):
        block = hd.Block("late", 0, 5, "r", "Command deck", "x", outfit="sleepless_watch")  # unlock 6
        assert hd.effective_outfit(_row(base="winter_patrol"), block, 3, cat) == ("winter_patrol", "auto")

    def test_since_locked_request_and_base_degrade_safely(self, cat):
        # Affection dropped below the request and the base: Blazing Star.
        assert hd.effective_outfit(_row(base="indigo_oath", requested="immaculate_service"), None, 1, cat) == (
            w.DEFAULT_OUTFIT, "auto",
        )

    def test_legacy_ids_map_forward(self, cat):
        assert hd.effective_outfit(_row(requested="starlit_vow"), None, 9, cat) == ("indigo_oath", "commander")


@pytest.fixture
def stubbed(cfg):
    """her_now with a fixed row and clock; returns a setter for the row."""
    state = SimpleNamespace(row=_row(day=MONDAY), hour=15)

    async def ensure(user_id, level, *, mood=None):
        return state.row

    with patch("app.wardrobe.ensure_today", new=ensure), \
         patch("app.her_day.now_local", side_effect=lambda: datetime(2026, 10, 5, state.hour, 0)):
        yield state


class TestHerNow:
    async def test_roster_location_and_kit(self, stubbed, cfg):
        stubbed.hour = 15
        now = await hd.her_now("claude", 5)
        block = hd.current_block(hd.build_day(cfg, MONDAY, 5, seed="claude"), 15)
        assert now.block == block
        assert (now.location, now.activity) == (block.location, block.activity)
        assert now.override is None
        assert now.outfit_reason == "A duty day."

    async def test_gaming_override(self, stubbed):
        now = await hd.her_now("claude", 5, game_active=True)
        assert now.override == "gaming"
        assert now.location == "Rec room"
        assert "your match" in now.activity
        assert now.status(5)["override"] == "gaming"
        assert now.status(0)["location"] == "Rec room"  # overrides are never "Off duty"

    async def test_mission_override_suits_her_up(self, stubbed):
        stubbed.row = _row(requested="cerulean_breaker", day=MONDAY)
        now = await hd.her_now("claude", 5, mission="extraction at Slovakia")
        assert now.override == "mission"
        assert (now.location, now.activity) == ("Deployed", "extraction at Slovakia")
        assert now.outfit.id == "blazing_star"

    async def test_no_block_means_the_elmo(self, stubbed):
        with patch("app.her_day.build_day", return_value=[]):
            now = await hd.her_now("claude", 5)
        assert (now.location, now.activity) == ("The Elmo", "")
        assert now.location_line().startswith("The Elmo.")
        assert now.status(5) == {"location": "The Elmo", "activity": "", "label": "The Elmo",
                                 "slot": None, "override": None}

    async def test_lines_and_schedule(self, stubbed):
        stubbed.hour = 15
        now = await hd.her_now("claude", 5)
        assert "you still answer him at once" in now.location_line()
        assert now.outfit_line(5).startswith(now.outfit.name)
        sched = now.schedule(5)
        assert sum(s["current"] for s in sched) == 1
        assert sched[0]["start"] == "0000"
        assert sched[-1]["end"] == "0000"  # 22-24 renders as midnight
        status = now.status(5)
        assert status["label"] == f"{status['location']} · {status['activity']}"

    async def test_private_blocks_are_off_duty_for_strangers(self, stubbed):
        private = hd.Block("afternoon", 14, 18, "toy", "Lounge", "toy bike", private=True)
        with patch("app.her_day.build_day", return_value=[private]):
            now = await hd.her_now("claude", 1)
        assert now.status(1)["label"] == "Off duty"
        assert now.schedule(1)[0]["location"] == "Off duty"
        # The prompt still knows the truth — she knows what she's doing.
        assert "toy bike" in now.location_line()

    async def test_overrides_default_without_config(self, stubbed):
        with patch("app.her_day.load_personality", return_value={"costumes": load_personality()["costumes"]}):
            gaming = await hd.her_now("claude", 5, game_active=True)
            mission = await hd.her_now("claude", 5, mission="m")
        assert (gaming.location, gaming.activity) == ("Rec room", "")
        assert mission.location == "Deployed"


class TestActiveMission:
    def test_states(self):
        timer = SimpleNamespace(mission_description="recon")
        with patch("app.context.proactive", SimpleNamespace(mission_active=True, _mission_timer=timer)):
            assert hd.active_mission() == "recon"
        with patch("app.context.proactive", SimpleNamespace(mission_active=True, _mission_timer=None)):
            assert hd.active_mission() == "on mission"
        with patch("app.context.proactive", SimpleNamespace(mission_active=False)):
            assert hd.active_mission() is None


class TestRenderCostume:
    async def test_what_she_has_on(self, stubbed):
        stubbed.row = _row(requested="immaculate_service", day=MONDAY)
        assert await hd.render_costume("claude", 5) == "immaculate_service"

    async def test_failure_falls_back_to_keywords(self):
        with patch("app.her_day.her_now", new=AsyncMock(side_effect=RuntimeError("x"))):
            assert await hd.render_costume("claude", 5) is None

    async def test_tagless_stand_in_falls_back(self, stubbed):
        with patch("app.wardrobe.lookup", return_value=w.FALLBACK):
            assert await hd.render_costume("claude", 5) is None


class TestTeaTime:
    def _now(self, block_id="tea_counter", override=None):
        block = hd.Block("midday", 12, 14, block_id, "Lounge", "Tea Time counter duty")
        return hd.HerNow(day=MONDAY, hour=13, outfit=w.FALLBACK, outfit_source="auto",
                         outfit_reason="", location="Lounge", activity="x", block=block,
                         blocks=[block], override=override)

    def test_serves_when_he_orders(self):
        block = hd.tea_time_block(self._now(), "Can I get a coffee?", 3)
        assert block.startswith("TEA TIME:")
        assert "Racing Calm" in block and "mint" in block
        assert "made with him in mind" not in block

    def test_heartfelt_when_he_orders_her_special_at_five(self):
        assert "made with him in mind" in hd.tea_time_block(self._now(), "one Racing Calm please", 5)
        assert "made with him in mind" not in hd.tea_time_block(self._now(), "one Racing Calm please", 4)

    @pytest.mark.parametrize("now_kw,msg", [
        ({"block_id": "hangar_afternoon"}, "coffee?"),
        ({"override": "gaming"}, "coffee?"),
        ({}, "how was the briefing"),
    ])
    def test_silent_otherwise(self, now_kw, msg):
        assert hd.tea_time_block(self._now(**now_kw), msg, 9) == ""

    def test_no_block(self):
        now = hd.HerNow(day=MONDAY, hour=13, outfit=w.FALLBACK, outfit_source="auto",
                        outfit_reason="", location="The Elmo", activity="", block=None, blocks=[])
        assert hd.tea_time_block(now, "tea", 9) == ""

    def test_defaults_without_config(self):
        block = hd.tea_time_block(self._now(), "tea", 3, p={})
        assert "Your special: Racing Calm" in block
