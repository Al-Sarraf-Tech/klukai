"""The wardrobe: one canon catalog, a deterministic daily pick, and his
requests decided by code — never by the LLM."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import wardrobe as w
from app.personality import load_personality

SUNDAY = date(2026, 10, 4)
MONDAY = date(2026, 10, 5)


@pytest.fixture(scope="module")
def p() -> dict:
    return load_personality()


@pytest.fixture(scope="module")
def cat(p) -> dict:
    return w.catalog(p)


def _conn(*, fetchone=None, fetchall=(), error=None):
    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    cursor = MagicMock()
    cursor.fetchone = AsyncMock(return_value=fetchone)
    cursor.fetchall = AsyncMock(return_value=list(fetchall))
    conn.execute = AsyncMock(side_effect=error, return_value=cursor)
    return conn


# ── The catalog ─────────────────────────────────────────────────────────────


class TestCatalog:
    def test_canon_outfits_all_present_and_marked(self, cat):
        canon = {o.id for o in cat.values() if o.source == "canon"}
        assert canon == {
            "blazing_star", "speed_star", "astral_luminous",
            "cerulean_breaker", "immaculate_service", "indigo_oath",
        }

    def test_invented_ids_are_gone(self, cat):
        assert "midnight_sovereign" not in cat
        assert "starlit_vow" not in cat

    def test_every_outfit_has_tags_name_and_a_hairstyle(self, cat):
        for o in cat.values():
            assert o.image_tags, o.id
            assert o.name and o.blurb, o.id
            assert any(h in o.image_tags for h in ("ponytail", "hair down")), o.id

    def test_canon_tags_match_the_official_art(self, cat):
        assert "black bikini" in cat["cerulean_breaker"].image_tags
        assert "holding surfboard" in cat["cerulean_breaker"].image_tags
        assert "armor" not in cat["cerulean_breaker"].image_tags
        assert "checkered clothes" in cat["astral_luminous"].image_tags
        assert "gown" not in cat["astral_luminous"].image_tags
        assert "baseball cap" in cat["blazing_star"].image_tags
        assert "gold" not in cat["blazing_star"].image_tags
        assert "wedding dress" in cat["indigo_oath"].image_tags
        assert "maid headdress" in cat["immaculate_service"].image_tags
        assert "white bodysuit" in cat["speed_star"].image_tags

    def test_catalog_is_cached_per_personality_object(self, p):
        assert w.catalog(p) is w.catalog(p)
        assert w.catalog({"costumes": {"x": {}}}) is not w.catalog(p)

    def test_defaults_and_malformed_entries(self):
        cat = w.catalog({"costumes": {"bare_suit": {}, "junk": "nope"}})
        assert list(cat) == ["bare_suit"]
        o = cat["bare_suit"]
        assert (o.name, o.source, o.category, o.unlock_level) == ("Bare Suit", "canon", "duty", 0)
        assert (o.visible_at, o.open_at, o.deny) == (0, 0, False)
        assert w.catalog({}) == {}

    def test_blurb_is_first_sentence(self, cat):
        assert cat["blazing_star"].blurb == "Military-tactical outfit befitting an elite squad leader"

    def test_catalog_defaults_to_loaded_personality(self):
        assert "blazing_star" in w.catalog()


class TestIds:
    def test_canonical_and_legacy(self, cat):
        assert w.canonical_id("Blazing_Star ", cat) == "blazing_star"
        assert w.canonical_id("starlit_vow", cat) == "indigo_oath"
        assert w.canonical_id("midnight_sovereign", cat) == "formal_commission"
        assert w.canonical_id("bogus", cat) is None
        assert w.canonical_id(None, cat) is None
        assert w.canonical_id("", cat) is None
        assert w.canonical_id("speed_star") == "speed_star"

    def test_unlock_is_fail_closed(self, cat):
        assert w.is_unlocked("blazing_star", 0, cat)
        assert not w.is_unlocked("indigo_oath", 7, cat)
        assert w.is_unlocked("indigo_oath", 8, cat)
        assert w.is_unlocked("starlit_vow", 8, cat)
        assert not w.is_unlocked("bogus", 9, cat)
        assert not w.is_unlocked(None, 9)

    def test_visibility(self, cat):
        assert not w.is_visible(cat["indigo_oath"], 6)
        assert w.is_visible(cat["indigo_oath"], 7)
        assert not w.is_visible(cat["klukadile_pajamas"], 6)
        assert w.is_visible(cat["klukadile_pajamas"], 7)
        assert w.is_visible(cat["immaculate_service"], 0)

    def test_lookup_and_tags(self, cat):
        assert w.lookup("starlit_vow", cat).id == "indigo_oath"
        assert w.lookup("bogus", cat) is w.FALLBACK
        assert w.lookup(None) is w.FALLBACK
        assert "wedding dress" in w.image_tags("starlit_vow", cat)
        assert w.image_tags("bogus", cat) is None
        assert w.image_tags("bare", {"bare": w.FALLBACK}) is None
        assert w.image_tags("blazing_star")


# ── Occasions ───────────────────────────────────────────────────────────────


class TestOccasions:
    def test_fixed_dates(self, p):
        assert w.occasions_on(date(2026, 10, 31), p) == {"halloween"}
        assert w.occasions_on(date(2026, 4, 16), p) == {"day_416"}
        assert "christmas" in w.occasions_on(date(2026, 12, 22), p)
        assert w.occasions_on(SUNDAY, p) == frozenset()

    def test_commander_birthday_is_dynamic(self, p):
        assert "commander_birthday" in w.occasions_on(SUNDAY, p, "10-04")
        assert "commander_birthday" not in w.occasions_on(SUNDAY, p, "03-14")

    def test_wrapping_range(self):
        cfg = {"wardrobe_occasions": {"winter": ["12-30..01-02"], "none": None}}
        assert w.occasions_on(date(2027, 1, 1), cfg) == {"winter"}
        assert w.occasions_on(date(2026, 12, 31), cfg) == {"winter"}
        assert w.occasions_on(date(2026, 6, 1), cfg) == frozenset()

    def test_defaults_to_loaded_personality(self):
        assert w.occasions_on(date(2026, 10, 31)) == {"halloween"}


# ── The daily pick ──────────────────────────────────────────────────────────


class TestPickDailyOutfit:
    def test_deterministic_for_the_same_day_and_seed(self, cat):
        a = w.pick_daily_outfit(cat, SUNDAY, 5, weather={"temp_c": 15, "condition": "clear"}, seed="claude")
        b = w.pick_daily_outfit(cat, SUNDAY, 5, weather={"temp_c": 15, "condition": "clear"}, seed="claude")
        assert a == b

    def test_duty_day_is_blazing_star_at_level_zero(self, cat):
        oid, reason = w.pick_daily_outfit(cat, MONDAY, 0, weather={"temp_c": 15, "condition": "clear"}, seed="x")
        assert oid == "blazing_star"
        assert reason == "A duty day."

    def test_occasion_wins(self, cat, p):
        occ = w.occasions_on(date(2026, 10, 31), p)
        assert w.pick_daily_outfit(cat, date(2026, 10, 31), 5, occasions=occ) == (
            "black_cat_ops", "It's Halloween.",
        )

    def test_occasion_outfit_still_needs_the_unlock(self, cat, p):
        occ = w.occasions_on(date(2026, 10, 31), p)
        oid, _ = w.pick_daily_outfit(cat, date(2026, 10, 31), 2, occasions=occ)
        assert oid != "black_cat_ops"

    def test_wedding_gown_only_at_nine_on_white_day(self, cat, p):
        occ = w.occasions_on(date(2027, 3, 14), p)
        assert w.pick_daily_outfit(cat, date(2027, 3, 14), 9, occasions=occ)[0] == "indigo_oath"
        assert w.pick_daily_outfit(cat, date(2027, 3, 14), 8, occasions=occ)[0] != "indigo_oath"

    def test_freezing_puts_her_in_the_coat(self, cat):
        oid, reason = w.pick_daily_outfit(cat, SUNDAY, 0, weather={"temp_c": -4, "condition": "cloudy"}, seed="s")
        assert oid == "winter_patrol"
        assert reason == "It's -4°C."

    def test_rain_shell_needs_rain(self, cat):
        for seed in "abcdefghij":
            oid, _ = w.pick_daily_outfit(cat, SUNDAY, 0, weather={"temp_c": 15, "condition": "clear"}, seed=seed)
            assert oid != "rain_shell"

    def test_rain_reason(self, cat):
        picks = {
            w.pick_daily_outfit(cat, SUNDAY, 0, weather={"temp_c": 15, "condition": "rain"}, seed=s)
            for s in "abcdefghijklmnop"
        }
        assert ("rain_shell", "It's raining.") in picks

    def test_beach_needs_heat(self, cat):
        hot = {
            w.pick_daily_outfit(cat, SUNDAY, 3, weather={"temp_c": 31, "condition": "clear"}, seed=s)[0]
            for s in "abcdefghijklmnop"
        }
        assert "cerulean_breaker" in hot
        cool = {
            w.pick_daily_outfit(cat, SUNDAY, 3, weather={"temp_c": 18, "condition": "clear"}, seed=s)[0]
            for s in "abcdefghijklmnop"
        }
        assert "cerulean_breaker" not in cool

    def test_seasonal_fallback_without_weather(self, cat):
        # January without a live reading: the seasonal temperature (0 °C) applies.
        oid, _ = w.pick_daily_outfit(cat, date(2027, 1, 10), 0, seed="s")
        assert oid == "winter_patrol"
        # Weather present but temperature missing → also the seasonal value.
        oid, _ = w.pick_daily_outfit(cat, date(2027, 1, 10), 0, weather={"condition": "cloudy"}, seed="s")
        assert oid == "winter_patrol"

    def test_mood_favorite_and_novelty(self):
        cat = w.catalog({"costumes": {
            "a": {"daily": {"weight": 1.0, "moods": ["tender"]}, "category": "off_duty"},
            "b": {"daily": {"weight": 1.0}, "category": "off_duty"},
        }})
        assert w.pick_daily_outfit(cat, SUNDAY, 0, mood="tender", seed="s") == ("a", "She's feeling tender.")
        assert w.pick_daily_outfit(cat, SUNDAY, 0, favorite="b", seed="s") == (
            "b", "He likes this one — not that it was a factor.",
        )
        # Worn yesterday (-2) loses to the other one; worn earlier (-1) too.
        assert w.pick_daily_outfit(cat, SUNDAY, 0, recent=("a",), seed="s")[0] == "b"
        assert w.pick_daily_outfit(cat, SUNDAY, 0, recent=("x", "b"), seed="s")[0] == "a"

    def test_no_reasons_is_her_call_and_empty_is_default(self):
        cat = w.catalog({"costumes": {"a": {"daily": {"weight": 1.0}}}})
        assert w.pick_daily_outfit(cat, SUNDAY, 0) == ("a", "Her call.")
        assert w.pick_daily_outfit({}, SUNDAY, 0) == (w.DEFAULT_OUTFIT, w.DEFAULT_REASON)

    def test_unknown_condition_reason(self):
        cat = w.catalog({"costumes": {"a": {"daily": {"conditions": ["hail"]}}}})
        assert w.pick_daily_outfit(cat, SUNDAY, 0, weather={"temp_c": 5, "condition": "hail"}) == (
            "a", "The weather.",
        )

    def test_avoid_conditions_and_bounds(self):
        assert not w._weather_ok({"avoid_conditions": ["rain"]}, 10, "rain")
        assert not w._weather_ok({"temp_min": 10}, 5, None)
        assert not w._weather_ok({"temp_max": 10}, 15, None)
        assert w._weather_ok({}, 15, None)


class TestLayersAndLines:
    def test_layers(self, cat):
        bs = cat["blazing_star"]
        assert "hair down" in w.layer_note(bs, 23, 3)
        assert w.layer_note(bs, 2, 2) == ""
        assert w.layer_note(bs, 18, 2) == "jacket off, gear partially stowed"
        assert w.layer_note(bs, 18, 1) == ""
        assert w.layer_note(cat["indigo_oath"], 23, 9) == ""
        assert w.layer_note(cat["immaculate_service"], 23, 9) == ""  # hair already down
        assert w.layer_note(cat["night_ride"], 18, 5) == ""

    def test_outfit_line_by_source(self, cat):
        bs = cat["blazing_star"]
        auto = w.outfit_line(bs, "A duty day.", "auto", 10, 5)
        assert auto.startswith("Blazing Star — Military-tactical outfit")
        assert auto.endswith("Why today: A duty day.")
        assert "your decision" in w.outfit_line(bs, "", "commander", 10, 5)
        assert "Kit for what you're doing" in w.outfit_line(bs, "", "roster", 10, 5)
        assert "(hair down" in w.outfit_line(bs, "x", "auto", 23, 5)

    def test_payload(self, cat):
        out = w.outfit_payload(cat["winter_patrol"], reason="It's 2°C.", source="auto", level=4)
        assert out == {
            "id": "winter_patrol", "name": "Winter Patrol", "blurb": out["blurb"],
            "category": "weather", "source": "auto", "reason": "It's 2°C.",
            "canon": False, "level_ok": True,
        }
        assert w.outfit_payload(cat["indigo_oath"], reason="x", source="commander", level=8)["reason"] == ""


# ── Requests ────────────────────────────────────────────────────────────────


class TestDetectOutfitRequest:
    @pytest.mark.parametrize("msg,expected", [
        ("Wear the maid outfit for me", "immaculate_service"),
        ("can you put on your wedding dress?", "indigo_oath"),
        ("would you wear the battle maid uniform today", "immaculate_service"),
        ("change into something casual", "off_duty_cap"),
        ("switch to the rider suit, let's ride", "speed_star"),
        ("I want to see you in a bikini", "cerulean_breaker"),
        ("put on your glasses", "drawing_night"),
        ("wear the Indigo Oath", "indigo_oath"),
        ("wear your coat, it's cold", "winter_patrol"),
        ("try on the crocodile onesie", "klukadile_pajamas"),
        ("wear the coat over the maid uniform", "winter_patrol"),
        ("wear the maid dress", "immaculate_service"),
    ])
    def test_detects(self, msg, expected):
        assert w.detect_outfit_request(msg) == expected

    @pytest.mark.parametrize("msg", [
        "don't wear the maid outfit",
        "never put on the wedding dress",
        "do you ever wear the maid outfit?",
        "did you wear a bikini on the mission",
        "what are you wearing?",
        "the maid outfit is ridiculous",
        "wear something",
        "I'll see you in the morning",
    ])
    def test_ignores(self, msg):
        assert w.detect_outfit_request(msg) is None

    def test_explicit_catalog(self):
        cat = w.catalog({"costumes": {"x": {"name": "Zed Suit", "aliases": ["zed"]}}})
        assert w.detect_outfit_request("wear the zed", cat) == "x"
        assert w.detect_outfit_request("wear the zed suit", cat) == "x"


def _decide(cat, oid, level, *, current="blazing_star", changes=0, occ=frozenset(), hour=12):
    return w.decide_request(
        cat[oid], level, current_id=current, request_changes=changes, occasions_today=occ, hour=hour,
    )


class TestDecideRequest:
    def test_onesie_is_always_denied(self, cat):
        assert _decide(cat, "klukadile_pajamas", 9, hour=2).decision == "deny"

    def test_wedding_gown_unacknowledged_below_seven(self, cat):
        assert _decide(cat, "indigo_oath", 6).decision == "unacknowledged"

    def test_already_wearing(self, cat):
        assert _decide(cat, "blazing_star", 5).decision == "already"

    @pytest.mark.parametrize("level,needle", [(1, "Refuse without"), (3, "Not yet"), (6, "trust")])
    def test_locked_bands(self, cat, level, needle):
        o = _decide(cat, "immaculate_service" if level < 5 else "formal_commission", level)
        if level == 6:  # formal_commission unlocks at 6 — use the wedding gown at 7 instead
            o = _decide(cat, "indigo_oath", 7)
        assert o.decision == "locked"
        assert needle in o.note

    def test_low_affection_refuses_non_practical(self, cat):
        assert _decide(cat, "speed_star", 2).decision == "refuse"
        assert _decide(cat, "winter_patrol", 0).decision == "comply"
        assert _decide(cat, "range_kit", 0).decision == "comply"

    def test_oath_not_today_below_nine(self, cat):
        assert _decide(cat, "indigo_oath", 8).decision == "not_today"
        assert _decide(cat, "indigo_oath", 8, occ=frozenset({"white_day"})).decision == "comply"
        assert _decide(cat, "indigo_oath", 9).decision == "comply"

    def test_seasonal_only_in_season(self, cat):
        assert _decide(cat, "black_cat_ops", 5).decision == "occasion_only"
        assert _decide(cat, "black_cat_ops", 5, occ=frozenset({"halloween"})).decision == "comply"

    def test_sleepwear_only_late(self, cat):
        assert _decide(cat, "sleepless_watch", 6, hour=14).decision == "not_now"
        assert _decide(cat, "sleepless_watch", 6, hour=23).decision == "comply"

    def test_daily_cap(self, cat):
        o = _decide(cat, "speed_star", 5, changes=w.MAX_REQUESTS_PER_DAY)
        assert o.decision == "limit"
        assert "mannequin" in o.note

    @pytest.mark.parametrize("level,needle", [
        (0, "curtly"), (3, "your decision"), (5, "already planning"), (7, "pleased"),
    ])
    def test_comply_bands(self, cat, level, needle):
        o = _decide(cat, "winter_patrol" if level < 3 else "blazing_star", level, current="range_kit")
        assert o.decision == "comply"
        assert needle in o.note


class TestRequestBlock:
    def test_granted(self, cat):
        block = w.request_block(w.RequestOutcome("immaculate_service", "comply", "Comply."), cat)
        assert "asked you to wear Immaculate Service" in block
        assert "you are now wearing Immaculate Service" in block
        assert "combat configuration" in block

    def test_refused_never_describes_changing(self):
        block = w.request_block(w.RequestOutcome("speed_star", "refuse", "No."))
        assert "NOT changing" in block

    def test_denied_does_not_name_it(self, cat):
        block = w.request_block(w.RequestOutcome("klukadile_pajamas", "deny", "Deny."), cat)
        assert "Klukadile" not in block
        assert "How you see it" not in block


# ── Persistence ─────────────────────────────────────────────────────────────

_NOW = datetime(2026, 10, 4, 9, 30)


@pytest.fixture
def clock():
    with patch("app.wardrobe.now_local", return_value=_NOW):
        yield


@pytest.fixture
def memory():
    mem = MagicMock()
    mem.recall_fact = AsyncMock(return_value=None)
    mem.store_fact = AsyncMock()
    with patch("app.context.memory", mem):
        yield mem


class TestEnsureToday:
    async def test_existing_row_is_loaded_and_cached(self, clock):
        row = ("speed_star", "Off-duty plans.", None, 0, json.dumps({"temp_c": 12, "condition": "clear"}))
        conn = _conn(fetchone=row)
        with patch("app.db.get_conn", return_value=conn):
            got = await w.ensure_today("claude", 5)
            again = await w.ensure_today("claude", 5)
        assert got == again
        assert got.base_outfit_id == "speed_star"
        assert got.weather == {"temp_c": 12, "condition": "clear"}
        assert conn.execute.await_count == 1  # second call served from cache

    async def test_first_touch_picks_and_persists(self, clock, memory):
        conn = _conn()
        conn_ac = _conn()
        picked = ("blazing_star", "A duty day.", None, 0, {"temp_c": 9.0, "condition": "cloudy"})
        conn.execute = AsyncMock(side_effect=[
            _cursor(fetchone=None),          # load: no row today
            _cursor(fetchall=[("winter_patrol",)]),  # recent
            _cursor(fetchone=picked),        # re-read after insert
        ])
        with patch("app.db.get_conn", return_value=conn), \
             patch("app.db.get_conn_autocommit", return_value=conn_ac), \
             patch("app.weather_client.fetch_weather", new=AsyncMock(return_value={"temp_c": 9.0, "condition": "cloudy"})), \
             patch("app.rituals.get_birthday", new=AsyncMock(return_value=None)):
            row = await w.ensure_today("claude", 3, mood="composed")
        assert row.base_outfit_id == "blazing_star"
        sql, params = conn_ac.execute.await_args.args
        assert "INSERT INTO companion_her_day" in sql
        assert params[0] == "claude" and params[1] == _NOW.date()
        assert json.loads(params[4]) == {"temp_c": 9.0, "condition": "cloudy"}

    async def test_lost_insert_race_still_returns_a_row(self, clock, memory):
        conn = _conn()
        conn.execute = AsyncMock(side_effect=[
            _cursor(fetchone=None), _cursor(fetchall=[]), _cursor(fetchone=None),
        ])
        with patch("app.db.get_conn", return_value=conn), \
             patch("app.db.get_conn_autocommit", return_value=_conn()), \
             patch("app.weather_client.fetch_weather", new=AsyncMock(return_value=None)), \
             patch("app.rituals.get_birthday", new=AsyncMock(return_value=None)):
            row = await w.ensure_today("claude", 0)
        assert row.day == _NOW.date()
        assert row.weather is None

    async def test_db_down_defaults_and_retries_later(self, clock):
        with patch("app.db.get_conn", side_effect=RuntimeError("pool")):
            row = await w.ensure_today("claude", 5)
        assert row.base_outfit_id == w.DEFAULT_OUTFIT
        # Within the retry window the failure row is served from cache…
        with patch("app.db.get_conn", side_effect=AssertionError("must not hit DB")):
            assert (await w.ensure_today("claude", 5)) == row
        # …and after it, the DB is tried again.
        with patch("app.wardrobe.time.monotonic", return_value=10**9), \
             patch("app.db.get_conn", return_value=_conn(fetchone=("speed_star", "x", None, 0, None))):
            assert (await w.ensure_today("claude", 5)).base_outfit_id == "speed_star"

    async def test_slow_weather_is_abandoned(self):
        async def slow():
            await asyncio.sleep(10)

        with patch("app.weather_client.fetch_weather", new=slow), \
             patch.object(w, "_WEATHER_TIMEOUT_S", 0.01):
            assert await w._quick_weather() is None


def _cursor(*, fetchone=None, fetchall=()):
    cur = MagicMock()
    cur.fetchone = AsyncMock(return_value=fetchone)
    cur.fetchall = AsyncMock(return_value=list(fetchall))
    return cur


class TestFavoriteAndHistory:
    async def test_legacy_favorite_is_migrated_forward(self, memory):
        memory.recall_fact.return_value = "starlit_vow"
        assert await w.favorite_outfit("claude") == "indigo_oath"
        memory.store_fact.assert_awaited_once_with("costume", "indigo_oath", user_id="claude")

    async def test_current_favorite_is_not_rewritten(self, memory):
        memory.recall_fact.return_value = "speed_star"
        assert await w.favorite_outfit("claude") == "speed_star"
        memory.store_fact.assert_not_awaited()

    async def test_no_favorite(self, memory):
        assert await w.favorite_outfit("claude") is None

    async def test_history_rows(self):
        rows = [
            (date(2026, 10, 3), "speed_star", "Off-duty plans.", "immaculate_service"),
            (date(2026, 10, 2), "starlit_vow", "x", None),
            (date(2026, 10, 1), "gone_now", "y", None),
        ]
        conn = _conn(fetchall=rows)
        with patch("app.db.get_conn", return_value=conn):
            out = await w.history("claude", 9999)
        assert out[0] == {"day": "2026-10-03", "outfit_id": "immaculate_service",
                          "reason": "Off-duty plans.", "requested": True}
        assert out[1]["outfit_id"] == "indigo_oath"
        assert out[2]["outfit_id"] == w.DEFAULT_OUTFIT
        assert conn.execute.await_args.args[1][1] == 400  # clamped


class TestHandleRequest:
    async def _run(self, row, oid, level, *, current="blazing_star", conn=None):
        w._today_cache["claude"] = (0.0, row, True)
        with patch("app.wardrobe.now_local", return_value=_NOW), \
             patch("app.rituals.get_birthday", new=AsyncMock(return_value=None)), \
             patch("app.db.get_conn_autocommit", return_value=conn or _conn()):
            return await w.handle_request("claude", oid, level, current_id=current)

    async def test_comply_records_and_updates_cache(self):
        conn = _conn()
        row = w.HerDayRow(_NOW.date(), "blazing_star", "A duty day.")
        out = await self._run(row, "speed_star", 5, conn=conn)
        assert out.decision == "comply"
        sql, params = conn.execute.await_args.args
        assert "UPDATE companion_her_day SET requested_outfit_id" in sql
        assert params == ("speed_star", 1, "claude", _NOW.date())
        cached = w._today_cache["claude"][1]
        assert (cached.requested_outfit_id, cached.request_changes) == ("speed_star", 1)

    async def test_refusal_changes_nothing(self):
        conn = _conn()
        row = w.HerDayRow(_NOW.date(), "blazing_star", "A duty day.")
        out = await self._run(row, "speed_star", 1, conn=conn)
        assert out.decision == "refuse"
        conn.execute.assert_not_awaited()

    async def test_db_failure_becomes_a_refusal(self):
        row = w.HerDayRow(_NOW.date(), "blazing_star", "A duty day.")
        out = await self._run(row, "speed_star", 5, conn=_conn(error=RuntimeError("down")))
        assert out.decision == "refuse"
        assert w._today_cache["claude"][1].requested_outfit_id is None


class TestSetRequested:
    async def test_ui_pick_does_not_count_toward_the_cap(self, clock):
        row = w.HerDayRow(_NOW.date(), "blazing_star", "A duty day.", request_changes=1)
        w._today_cache["claude"] = (0.0, row, True)
        conn = _conn()
        with patch("app.db.get_conn_autocommit", return_value=conn):
            await w.set_requested("claude", "immaculate_service", 5)
        assert conn.execute.await_args.args[1] == ("immaculate_service", 0, "claude", _NOW.date())
        cached = w._today_cache["claude"][1]
        assert (cached.requested_outfit_id, cached.request_changes) == ("immaculate_service", 1)


class TestDecidedOutfit:
    async def test_honoured_on_the_day(self, cat, memory):
        memory.recall_fact.return_value = json.dumps({"date": "2026-10-04", "outfit_id": "dress_uniform"})
        assert await w.decided_outfit("claude", SUNDAY, 5, cat) == "dress_uniform"
        memory.recall_fact.assert_awaited_once_with("outfit:tomorrow", user_id="claude")

    @pytest.mark.parametrize("raw", [
        None, "not json", json.dumps(["x"]),
        json.dumps({"date": "2026-10-05", "outfit_id": "dress_uniform"}),
        json.dumps({"date": "2026-10-04", "outfit_id": "bogus"}),
        json.dumps({"date": "2026-10-04", "outfit_id": "immaculate_service"}),  # locked at 2
    ])
    async def test_ignored(self, cat, memory, raw):
        memory.recall_fact.return_value = raw
        assert await w.decided_outfit("claude", SUNDAY, 2, cat) is None

    async def test_first_touch_uses_his_call(self, clock, memory):
        memory.recall_fact.return_value = json.dumps({"date": "2026-10-04", "outfit_id": "speed_star"})
        conn = _conn()
        conn.execute = AsyncMock(side_effect=[_cursor(fetchone=None), _cursor(fetchone=None)])
        conn_ac = _conn()
        with patch("app.db.get_conn", return_value=conn), \
             patch("app.db.get_conn_autocommit", return_value=conn_ac), \
             patch("app.weather_client.fetch_weather", new=AsyncMock(return_value=None)):
            row = await w.ensure_today("claude", 5)
        assert row.base_outfit_id == "speed_star"
        assert row.base_reason.startswith("His call")
