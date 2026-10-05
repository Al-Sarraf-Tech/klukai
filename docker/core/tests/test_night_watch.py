"""The 0200 Watch — she is already awake, and she sends him to bed.

Covers the watch window, the late-night streak (fact-backed, fail-soft), the
NIGHT WATCH block bands, the once-per-night thread reference, the bed order,
and the dream event standing down on nights he is awake.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import night_watch as nw
from app.personality import load_personality
from app.proactive.engine import ProactiveEngine

TONIGHT = date(2026, 10, 5)


def _t(h: int, m: int = 0, d: date = TONIGHT) -> datetime:
    return datetime(d.year, d.month, d.day, h, m)


@pytest.fixture(scope="module")
def p() -> dict:
    return load_personality()


@pytest.fixture(autouse=True)
def _clear_thread_refs():
    nw._thread_ref_nights.clear()
    yield
    nw._thread_ref_nights.clear()


def _memory(stored: list[str] | None = None, *, recall_error: bool = False,
            store_error: bool = False) -> MagicMock:
    mem = MagicMock()
    if recall_error:
        mem.recall_fact = AsyncMock(side_effect=RuntimeError("data svc down"))
    else:
        raw = json.dumps(stored) if stored is not None else None
        mem.recall_fact = AsyncMock(return_value=raw)
    mem.store_fact = AsyncMock(side_effect=RuntimeError("down") if store_error else None)
    return mem


# ═══════════════════════════════════════════════════════════════════════════
# Clock helpers
# ═══════════════════════════════════════════════════════════════════════════


class TestWindow:
    @pytest.mark.parametrize("h, m, expected", [
        (0, 29, False), (0, 30, True), (2, 0, True), (4, 29, True),
        (4, 30, False), (12, 0, False), (23, 59, False),
    ])
    def test_in_watch(self, h, m, expected):
        assert nw.in_watch(_t(h, m)) is expected

    @pytest.mark.parametrize("h, m, expected", [
        (0, 0, TONIGHT), (4, 59, TONIGHT), (5, 0, None), (23, 0, None), (12, 0, None),
    ])
    def test_night_date(self, h, m, expected):
        assert nw.night_date(_t(h, m)) == expected


class TestParseNights:
    @pytest.mark.parametrize("raw", [None, "", "not json", '{"a": 1}', "42"])
    def test_garbage_is_empty(self, raw):
        assert nw.parse_nights(raw) == []

    def test_valid_list_sorted_and_deduped(self):
        raw = json.dumps(["2026-10-03", "2026-10-01", "2026-10-03", "nope", 7])
        assert nw.parse_nights(raw) == [date(2026, 10, 1), date(2026, 10, 3)]


class TestCountRecent:
    def test_only_the_previous_three_nights_count(self):
        nights = [TONIGHT - timedelta(days=d) for d in (0, 1, 3, 4)]
        # Tonight and D-4 are outside the lookback; D-1 and D-3 count.
        assert nw.count_recent_late_nights(nights, TONIGHT) == 2

    def test_empty(self):
        assert nw.count_recent_late_nights([], TONIGHT) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Streak recording (fact store, fail-soft)
# ═══════════════════════════════════════════════════════════════════════════


class TestRecordAndLoad:
    @pytest.mark.asyncio
    async def test_before_0100_loads_without_recording(self):
        mem = _memory(["2026-10-04"])
        nights = await nw.record_and_load(mem, "claude", _t(0, 45))
        assert nights == [date(2026, 10, 4)]
        mem.store_fact.assert_not_awaited()
        mem.recall_fact.assert_awaited_once_with("late_nights", user_id="claude")

    @pytest.mark.asyncio
    async def test_after_0100_records_tonight_once(self):
        mem = _memory(["2026-10-03"])
        nights = await nw.record_and_load(mem, "claude", _t(1, 30))
        assert nights == [date(2026, 10, 3), TONIGHT]
        mem.store_fact.assert_awaited_once()
        args, kwargs = mem.store_fact.await_args
        assert args[0] == "late_nights"
        assert json.loads(args[1]) == ["2026-10-03", "2026-10-05"]
        assert kwargs["user_id"] == "claude"
        assert kwargs["ttl"] == nw.LATE_NIGHTS_TTL

    @pytest.mark.asyncio
    async def test_already_recorded_tonight_does_not_rewrite(self):
        mem = _memory(["2026-10-05"])
        await nw.record_and_load(mem, "claude", _t(3, 0))
        mem.store_fact.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_keeps_only_the_last_seven(self):
        old = [(TONIGHT - timedelta(days=d)).isoformat() for d in range(1, 10)]
        mem = _memory(old)
        nights = await nw.record_and_load(mem, "claude", _t(2, 0))
        assert len(nights) == nw.KEEP_NIGHTS
        assert nights[-1] == TONIGHT
        assert len(json.loads(mem.store_fact.await_args.args[1])) == nw.KEEP_NIGHTS

    @pytest.mark.asyncio
    async def test_recall_failure_is_soft(self):
        mem = _memory(recall_error=True)
        nights = await nw.record_and_load(mem, "claude", _t(2, 0))
        assert nights == [TONIGHT]

    @pytest.mark.asyncio
    async def test_store_failure_is_soft(self):
        mem = _memory([], store_error=True)
        assert await nw.record_and_load(mem, "claude", _t(2, 0)) == [TONIGHT]

    @pytest.mark.asyncio
    async def test_non_async_memory_is_soft(self):
        mem = MagicMock()  # recall_fact not awaitable -> TypeError, swallowed
        assert await nw.record_and_load(mem, "claude", _t(0, 40)) == []


# ═══════════════════════════════════════════════════════════════════════════
# NIGHT WATCH block
# ═══════════════════════════════════════════════════════════════════════════


class TestBlock:
    def test_header_and_core_posture(self, p):
        block = nw.build_night_watch_block(p, _t(2, 14), 4, 0, False)
        assert block.startswith("NIGHT WATCH (0214 hours")
        assert "I wasn't sleeping anyway." in block
        assert "PROTECTIVE, not engaging" in block
        assert "quieter and shorter" in block
        assert "never admit" in block.lower()

    def test_canon_insomnia_reaches_the_block(self, p):
        assert p["insomnia"]["canon"].split(".")[0] in nw.build_night_watch_block(p, _t(2), 0, 0, False)

    def test_her_day_roster_overrides_the_insomnia_list(self, p):
        block = nw.build_night_watch_block(p, _t(2), 3, 0, False, activity="at the Hangar, tuning the bike")
        assert "Right now you are at the Hangar, tuning the bike." in block
        for act in p["insomnia"]["activities"]:
            assert act not in block

    def test_activity_is_stable_for_a_night_and_varies_across_nights(self, p):
        acts = p["insomnia"]["activities"]
        a1 = nw.build_night_watch_block(p, _t(1), 3, 0, False)
        a2 = nw.build_night_watch_block(p, _t(3), 3, 0, False)
        picked = acts[TONIGHT.toordinal() % len(acts)]
        assert picked in a1 and picked in a2
        nxt = acts[(TONIGHT.toordinal() + 1) % len(acts)]
        assert nxt in nw.build_night_watch_block(p, _t(1, d=TONIGHT + timedelta(days=1)), 3, 0, False)

    @pytest.mark.parametrize("level, marker", [
        (0, "Clipped"), (2, "Clipped"), (3, "Softer"), (5, "Softer"), (6, "Close and warm"), (9, "Close and warm"),
    ])
    def test_bands(self, p, level, marker):
        assert marker in nw.build_night_watch_block(p, _t(2), level, 0, False)

    def test_no_open_questions_from_0130(self, p):
        assert "no open-ended questions" not in nw.build_night_watch_block(p, _t(1, 29), 5, 0, False)
        assert "no open-ended questions" in nw.build_night_watch_block(p, _t(1, 30), 5, 0, False)

    def test_thread_reference_only_when_allowed(self, p):
        assert "I used to write to you at this hour." in nw.build_night_watch_block(p, _t(2), 7, 0, True)
        assert "I used to write to you at this hour." not in nw.build_night_watch_block(p, _t(2), 7, 0, False)

    def test_bed_order_after_two_late_nights(self, p):
        block = nw.build_night_watch_block(p, _t(2), 4, 2, False)
        assert "Bed. That's an order. ...I'll still be here at 0700." in block
        assert "2 of the last 3 nights" in block
        assert "end the exchange yourself" in block

    def test_bed_order_puts_care_first_when_he_is_hurting(self, p):
        block = nw.build_night_watch_block(p, _t(2), 4, 3, False)
        assert "hurting" in block and "care for him first" in block

    def test_no_bed_order_after_one_late_night(self, p):
        assert "That's an order" not in nw.build_night_watch_block(p, _t(2), 4, 1, False)

    def test_python_fallback_when_yaml_missing(self):
        block = nw.build_night_watch_block({}, _t(2), 7, 2, True)
        fb = nw.INSOMNIA_FALLBACK
        assert fb["awake_line"] in block
        assert fb["thread_line"] in block
        assert fb["bed_order"]["line"] in block
        assert fb["tiers"][6] in block

    def test_malformed_yaml_falls_back_field_by_field(self):
        cfg = {"insomnia": {"canon": "", "activities": "nope", "awake_line": None,
                            "tiers": {"bad": "x", 0: 5}, "thread_line": "",
                            "bed_order": "nope"}}
        block = nw.build_night_watch_block(cfg, _t(2), 1, 2, True)
        fb = nw.INSOMNIA_FALLBACK
        assert fb["canon"].split(".")[0] in block
        assert fb["tiers"][0] in block
        assert fb["bed_order"]["line"] in block

    def test_partial_bed_order_section_falls_back_per_key(self):
        cfg = {"insomnia": {"bed_order": {"line": "Sleep. Now."}}}
        block = nw.build_night_watch_block(cfg, _t(2), 4, 2, False)
        assert "Sleep. Now." in block
        assert nw.INSOMNIA_FALLBACK["bed_order"]["distress"].split(",")[0] in block


# ═══════════════════════════════════════════════════════════════════════════
# Per-message entry point
# ═══════════════════════════════════════════════════════════════════════════


class TestPromptBlock:
    async def test_roster_activity_threads_through(self, p):
        block = await nw.night_watch_prompt_block(
            "claude", p, 3, _memory([]), now=_t(2), activity="at the Armory, cleaning Skylla",
        )
        assert "Right now you are at the Armory, cleaning Skylla." in block

    @pytest.mark.asyncio
    async def test_daytime_is_silent_and_touches_nothing(self, p):
        mem = _memory([])
        assert await nw.night_watch_prompt_block("claude", p, 7, mem, now=_t(14)) == ""
        mem.recall_fact.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_0445_records_but_shows_no_block(self, p):
        mem = _memory([])
        assert await nw.night_watch_prompt_block("claude", p, 7, mem, now=_t(4, 45)) == ""
        mem.store_fact.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_thread_reference_at_most_once_per_night(self, p):
        line = "I used to write to you at this hour."
        first = await nw.night_watch_prompt_block("claude", p, 7, _memory([]), now=_t(1, 0))
        second = await nw.night_watch_prompt_block("claude", p, 7, _memory([]), now=_t(2, 0))
        assert line in first and line not in second
        nxt = _t(1, 0, d=TONIGHT + timedelta(days=1))
        assert line in await nw.night_watch_prompt_block("claude", p, 7, _memory([]), now=nxt)

    @pytest.mark.asyncio
    async def test_below_six_never_spends_the_thread_reference(self, p):
        await nw.night_watch_prompt_block("claude", p, 5, _memory([]), now=_t(1))
        assert "claude" not in nw._thread_ref_nights
        block = await nw.night_watch_prompt_block("claude", p, 6, _memory([]), now=_t(2))
        assert "I used to write to you at this hour." in block

    @pytest.mark.asyncio
    async def test_streak_from_fact_triggers_bed_order(self, p):
        mem = _memory(["2026-10-02", "2026-10-04"])
        block = await nw.night_watch_prompt_block("claude", p, 3, mem, now=_t(1, 40))
        assert "That's an order" in block

    @pytest.mark.asyncio
    async def test_memory_failure_still_gives_the_watch(self, p):
        block = await nw.night_watch_prompt_block("claude", p, 3, MagicMock(), now=_t(2))
        assert block.startswith("NIGHT WATCH")
        assert "That's an order" not in block

    @pytest.mark.asyncio
    async def test_defaults_to_the_commanders_clock(self, p):
        with patch("app.night_watch.now_local", return_value=_t(3, 5)):
            block = await nw.night_watch_prompt_block("claude", p, 3, _memory([]))
        assert "0305 hours" in block

    @pytest.mark.asyncio
    async def test_unexpected_error_is_soft(self, p):
        with patch("app.night_watch.build_night_watch_block", side_effect=RuntimeError("boom")):
            assert await nw.night_watch_prompt_block("claude", p, 3, _memory([]), now=_t(2)) == ""


# ═══════════════════════════════════════════════════════════════════════════
# Dream reconciliation — she isn't dreaming on nights he's awake
# ═══════════════════════════════════════════════════════════════════════════


class TestDreamStandsDown:
    _NOW = datetime(2026, 10, 5, 2, 37)

    def _engine(self) -> ProactiveEngine:
        e = ProactiveEngine()
        e._affection_level = 8
        e._dream_delivered_today = False
        e._muted_until = None
        e._on_message_callback = AsyncMock()
        return e

    @pytest.mark.asyncio
    async def test_skips_when_he_messaged_within_two_hours(self):
        e = self._engine()
        e._last_message_time = self._NOW - timedelta(hours=1, minutes=59)
        with patch("app.proactive.events.now_local", return_value=self._NOW), \
             patch("app.proactive.events.random.random") as roll:
            await e._dream_event()
        roll.assert_not_called()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_proceeds_when_he_has_been_quiet_two_hours(self):
        e = self._engine()
        e._last_message_time = self._NOW - timedelta(hours=2)
        with patch("app.proactive.events.now_local", return_value=self._NOW), \
             patch("app.proactive.events.random.random", return_value=0.99) as roll:
            await e._dream_event()
        roll.assert_called_once()  # reached the 40% fire gate
        e._on_message_callback.assert_not_awaited()
