"""Tests for app/op_brief.py — the Operation Brief.

She prepares him for his real-life events: a brief the evening before, a
send-off that morning, a debrief ask that evening. Everything external is
mocked: the DB (get_conn), the deferred rail, the clock (now_local), and the
context singletons (affection, proactive, router, ws).

Fixed clock: Monday 2026-10-05 12:00 local unless a test says otherwise.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import op_brief  # noqa: E402
from app.op_brief import When  # noqa: E402

NOW = datetime(2026, 10, 5, 12, 0)  # Monday
EVENT_ID = "11111111-2222-3333-4444-555555555555"


# ── fakes ────────────────────────────────────────────────────────────────────


class _FakeConn:
    """Records SQL; serves queued fetchone results and one fetchall result."""

    def __init__(self, fetchone=None, fetchall=None, raise_on_execute=False):
        self.executed: list[tuple] = []
        self._fetchone = list(fetchone) if isinstance(fetchone, list) else [fetchone]
        self._fetchall = fetchall or []
        self._raise = raise_on_execute
        self.commits = 0

    async def execute(self, sql, params=None):
        if self._raise:
            raise RuntimeError("db down")
        self.executed.append((sql, params))
        cur = MagicMock()
        nxt = self._fetchone.pop(0) if self._fetchone else None
        cur.fetchone = AsyncMock(return_value=nxt)
        cur.fetchall = AsyncMock(return_value=self._fetchall)
        return cur

    async def commit(self):
        self.commits += 1


def _patch_conn(conn):
    @asynccontextmanager
    async def _cm():
        yield conn

    return patch("app.op_brief.get_conn", _cm)


def _commitment(**overrides) -> dict:
    base = {
        "kind": "event",
        "what": "job interview at the bank",
        "when_hint": "thursday at 9am",
        "event_date": "2026-10-08",
        "event_time": "09:00",
        "time_exact": True,
        "variant": "normal",
        "dedupe_key": "2026-10-08|interview",
        "confidence": 0.9,
    }
    base.update(overrides)
    return base


def _cfg(**overrides) -> op_brief.BriefConfig:
    """A config built from the real defaults, so tests don't depend on YAML."""
    with patch("app.personality.load_personality", return_value={}):
        cfg = op_brief.load_config()
    if overrides:
        cfg = op_brief.BriefConfig(**{**cfg.__dict__, **overrides})
    return cfg


# ═════════════════════════════════════════════════════════════════════════
# classification + normalization
# ═════════════════════════════════════════════════════════════════════════


class TestClassify:
    @pytest.mark.parametrize("what", [
        "grandmother's funeral", "surgery on my knee", "court hearing", "the breakup talk",
    ])
    def test_sensitive_keywords_win(self, what):
        assert op_brief.classify(what, "normal") == "sensitive"

    def test_llm_can_raise_sensitivity_without_a_keyword(self):
        assert op_brief.classify("meeting with my father", "Sensitive ") == "sensitive"

    @pytest.mark.parametrize("what", ["dentist appointment", "blood test", "MRI scan", "Dr. Patel"])
    def test_medical(self, what):
        assert op_brief.classify(what) == "medical"

    def test_normal(self):
        assert op_brief.classify("job interview", None) == "normal"


class TestCategory:
    def test_known_category(self):
        assert op_brief.category("the big interview at Google") == "interview"
        assert op_brief.category("final exam") == "exam"

    def test_unknown_falls_back_to_content_words(self):
        assert op_brief.category("my sister's wedding") == "sister's wedding"

    def test_nothing_left_is_event(self):
        assert op_brief.category("the") == "event"


class TestNormalizeWhat:
    def test_my_becomes_your(self):
        assert op_brief.normalize_what("My  sister's wedding.") == "your sister's wedding"

    def test_his_becomes_your(self):
        assert op_brief.normalize_what("his flight to Denver") == "your flight to Denver"

    def test_articles_dropped(self):
        assert op_brief.normalize_what("an exam") == "exam"
        assert op_brief.normalize_what("the interview") == "interview"

    def test_plain_kept(self):
        assert op_brief.normalize_what("job interview") == "job interview"


# ═════════════════════════════════════════════════════════════════════════
# resolve_when
# ═════════════════════════════════════════════════════════════════════════


class TestResolveWhen:
    @pytest.mark.parametrize("hint,expected", [
        ("tomorrow at 9am", When(date(2026, 10, 6), time(9, 0), True)),
        ("Tomorrow", When(date(2026, 10, 6), None, False)),
        ("tomorrow morning", When(date(2026, 10, 6), time(9, 0), False)),
        ("day after tomorrow", When(date(2026, 10, 7), None, False)),
        ("today at 3pm", When(date(2026, 10, 5), time(15, 0), True)),
        ("tonight", When(date(2026, 10, 5), time(19, 30), False)),
        ("this afternoon", When(date(2026, 10, 5), time(14, 0), False)),
        ("at 3", When(date(2026, 10, 5), time(15, 0), True)),
        ("noon tomorrow", When(date(2026, 10, 6), time(12, 0), True)),
        ("0900 hours thursday", When(date(2026, 10, 8), time(9, 0), True)),
        ("thursday 2:30 pm", When(date(2026, 10, 8), time(14, 30), True)),
        ("thursday at 2:30", When(date(2026, 10, 8), time(14, 30), True)),
        ("thursday 10:15am", When(date(2026, 10, 8), time(10, 15), True)),
        ("thursday at 10", When(date(2026, 10, 8), time(10, 0), True)),
        ("friday 13:30pm", When(date(2026, 10, 9), None, False)),
        ("in 3 days", When(date(2026, 10, 8), None, False)),
        ("in two days", When(date(2026, 10, 7), None, False)),
        ("in a week", When(date(2026, 10, 12), None, False)),
        ("2026-10-20", When(date(2026, 10, 20), None, False)),
        ("Oct. 12", When(date(2026, 10, 12), None, False)),
        ("12th of October", When(date(2026, 10, 12), None, False)),
        ("10/14", When(date(2026, 10, 14), None, False)),
        ("10/14/26", When(date(2026, 10, 14), None, False)),
        ("10/14/2026", When(date(2026, 10, 14), None, False)),
        ("the 20th", When(date(2026, 10, 20), None, False)),
        ("Thursday", When(date(2026, 10, 8), None, False)),
        ("on monday", When(date(2026, 10, 12), None, False)),
        ("this monday", When(date(2026, 10, 5), None, False)),
        ("next thursday", When(date(2026, 10, 15), None, False)),
        ("next monday", When(date(2026, 10, 12), None, False)),
    ])
    def test_resolves(self, hint, expected):
        assert op_brief.resolve_when(hint, NOW) == expected

    @pytest.mark.parametrize("hint", [
        None, "", "   ", 123, "next week", "soon", "at 25",
        "today at 9am",            # already happened
        "at 10",                   # today, already happened
        "2026-10-01",              # in the past
        "2026-02-30",              # not a date
        "october 2",               # past this year -> next year -> outside window
        "the 3rd",                 # next month's 3rd is 29 days out
        "in 30 days",              # outside the window
    ])
    def test_rejects(self, hint):
        assert op_brief.resolve_when(hint, NOW) is None

    def test_next_weekday_from_a_sunday_is_the_coming_one(self):
        sunday = datetime(2026, 10, 4, 12, 0)
        assert op_brief.resolve_when("next monday", sunday) == When(date(2026, 10, 5), None, False)

    def test_ordinal_rolls_into_next_year_in_december(self):
        assert op_brief.resolve_when("the 5th", datetime(2026, 12, 20, 9, 0)) == \
            When(date(2027, 1, 5), None, False)

    def test_ordinal_invalid_day_for_month(self):
        assert op_brief.resolve_when("the 31st", datetime(2026, 11, 5, 9, 0)) is None

    def test_month_day_rolls_into_next_year(self):
        assert op_brief.resolve_when("jan 3", datetime(2026, 12, 28, 9, 0)) == \
            When(date(2027, 1, 3), None, False)


# ═════════════════════════════════════════════════════════════════════════
# plan_slots
# ═════════════════════════════════════════════════════════════════════════


def _w(day: int, at: time | None = None, exact: bool = True) -> When:
    return When(date(2026, 10, day), at, exact if at else False)


class TestPlanSlots:
    def test_full_chain_with_morning_event(self):
        assert op_brief.plan_slots(_w(8, time(9, 0)), NOW) == [
            ("brief", datetime(2026, 10, 7, 20, 30)),
            ("sendoff", datetime(2026, 10, 8, 8, 5)),
            ("debrief", datetime(2026, 10, 8, 19, 0)),
        ]

    def test_full_chain_without_time(self):
        slots = op_brief.plan_slots(_w(8), NOW)
        assert [s for s, _ in slots] == ["brief", "sendoff", "debrief"]

    def test_sendoff_skipped_when_event_is_too_early(self):
        slots = op_brief.plan_slots(_w(8, time(8, 0)), NOW)
        assert [s for s, _ in slots] == ["brief", "debrief"]

    def test_debrief_moves_after_a_late_event(self):
        slots = dict(op_brief.plan_slots(_w(8, time(18, 30)), NOW))
        assert slots["debrief"] == datetime(2026, 10, 8, 20, 0)

    def test_debrief_rolls_to_next_morning_past_the_evening_cutoff(self):
        slots = dict(op_brief.plan_slots(_w(8, time(21, 30)), NOW))
        assert slots["debrief"] == datetime(2026, 10, 9, 10, 0)

    def test_debrief_rolls_when_the_event_crosses_midnight(self):
        slots = dict(op_brief.plan_slots(_w(8, time(23, 30)), NOW))
        assert slots["debrief"] == datetime(2026, 10, 9, 10, 0)

    def test_past_slots_are_skipped(self):
        assert op_brief.plan_slots(_w(5, time(15, 0)), NOW) == [
            ("debrief", datetime(2026, 10, 5, 19, 0)),
        ]

    def test_everything_past_is_empty(self):
        late = datetime(2026, 10, 5, 22, 0)
        assert op_brief.plan_slots(When(date(2026, 10, 5), time(19, 30), False), late) == []


# ═════════════════════════════════════════════════════════════════════════
# accept_event + when_of
# ═════════════════════════════════════════════════════════════════════════


class TestAcceptEvent:
    def test_accepts_and_builds_commitment(self):
        out = op_brief.accept_event(
            {"what": "My job interview at the bank", "when_hint": "thursday at 9am",
             "sensitivity": "normal", "confidence": 0.9}, NOW)
        assert out == {
            "kind": "event", "what": "your job interview at the bank",
            "when_hint": "thursday at 9am", "event_date": "2026-10-08",
            "event_time": "09:00", "time_exact": True, "variant": "normal",
            "dedupe_key": "2026-10-08|interview", "confidence": 0.9,
        }

    def test_untimed_event_has_no_time(self):
        out = op_brief.accept_event(
            {"what": "funeral", "when_hint": "friday", "confidence": 0.8}, NOW)
        assert out["event_time"] is None and out["variant"] == "sensitive"

    @pytest.mark.parametrize("raw", [
        "not a dict",
        {"when_hint": "tomorrow", "confidence": 0.9},
        {"what": "  ", "when_hint": "tomorrow", "confidence": 0.9},
        {"what": "exam", "when_hint": "tomorrow", "confidence": "high"},
        {"what": "exam", "when_hint": "tomorrow", "confidence": 0.74},
        {"what": "exam", "when_hint": "next week", "confidence": 0.9},
    ])
    def test_rejects(self, raw):
        assert op_brief.accept_event(raw, NOW) is None

    def test_when_of_round_trips(self):
        assert op_brief.when_of(_commitment()) == When(date(2026, 10, 8), time(9, 0), True)
        assert op_brief.when_of(_commitment(event_time=None, time_exact=False)) == \
            When(date(2026, 10, 8), None, False)


# ═════════════════════════════════════════════════════════════════════════
# storage
# ═════════════════════════════════════════════════════════════════════════


class TestStoreEvent:
    async def test_inserts_with_null_followup(self):
        conn = _FakeConn(fetchone=[None, ("new-id",)])
        with _patch_conn(conn):
            out = await op_brief.store_event(_commitment(), "claude")
        assert out == "new-id"
        insert_sql, params = conn.executed[1]
        assert "VALUES (%s, %s, %s, NULL)" in insert_sql  # never a promise follow-up
        assert params[0] == "claude" and '"kind": "event"' in params[2]
        assert conn.commits == 1

    async def test_duplicate_open_event_is_skipped(self):
        conn = _FakeConn(fetchone=[("existing",)])
        with _patch_conn(conn):
            assert await op_brief.store_event(_commitment(), "claude") is None
        assert len(conn.executed) == 1
        assert "dedupe_key" in conn.executed[0][0] and "resolved_at IS NULL" in conn.executed[0][0]

    async def test_no_row_returned(self):
        with _patch_conn(_FakeConn(fetchone=[None, None])):
            assert await op_brief.store_event(_commitment(), "claude") is None

    async def test_db_failure_is_soft(self):
        with _patch_conn(_FakeConn(raise_on_execute=True)):
            assert await op_brief.store_event(_commitment(), "claude") is None


class TestOpenEvents:
    async def test_parses_rows(self):
        rows = [("id1", "exam", '{"kind": "event", "what": "exam"}'),
                ("id2", "flight", {"kind": "event", "what": "flight"}),
                ("id3", "broken", "{not json"),
                ("id4", "odd", None)]
        conn = _FakeConn(fetchall=rows)
        with _patch_conn(conn):
            out = await op_brief.open_events("claude")
        assert [e["id"] for e in out] == ["id1", "id2", "id3", "id4"]
        assert out[0]["commitment"]["what"] == "exam"
        assert out[2]["commitment"] == {} and out[3]["commitment"] == {}
        assert "followup_sent_at IS NULL" in conn.executed[0][0]

    async def test_db_failure_is_soft(self):
        with _patch_conn(_FakeConn(raise_on_execute=True)):
            assert await op_brief.open_events("claude") == []


class TestLoadEvent:
    async def test_malformed_id_never_hits_db(self):
        conn = _FakeConn()
        with _patch_conn(conn):
            assert await op_brief.load_event("not-a-uuid", "claude") is None
        assert conn.executed == []

    @pytest.mark.parametrize("row", [
        None,
        ("x", _commitment(), datetime(2026, 10, 1), None),   # cancelled
        ("x", _commitment(), None, datetime(2026, 10, 1)),   # chain finished
        ("x", {"kind": "promise", "event_date": "2026-10-08"}, None, None),
        ("x", {"kind": "event"}, None, None),                # no date
    ])
    async def test_not_live(self, row):
        with _patch_conn(_FakeConn(fetchone=[row])):
            assert await op_brief.load_event(EVENT_ID, "claude") is None

    async def test_live_event(self):
        with _patch_conn(_FakeConn(fetchone=[("x", _commitment(), None, None)])):
            assert await op_brief.load_event(EVENT_ID, "claude") == _commitment()

    async def test_db_error_propagates_for_rail_retry(self):
        with _patch_conn(_FakeConn(raise_on_execute=True)):
            with pytest.raises(RuntimeError):
                await op_brief.load_event(EVENT_ID, "claude")


class TestStamps:
    async def test_mark_complete(self):
        conn = _FakeConn()
        with _patch_conn(conn):
            await op_brief.mark_complete(EVENT_ID, "claude")
        sql, params = conn.executed[0]
        assert "followup_sent_at = NOW()" in sql and params == (EVENT_ID, "claude")
        assert conn.commits == 1

    async def test_mark_cancelled_truncates_note(self):
        conn = _FakeConn()
        with _patch_conn(conn):
            await op_brief.mark_cancelled(EVENT_ID, "claude", "x" * 500)
        sql, params = conn.executed[0]
        assert "sentiment = 'cancelled'" in sql and len(params[0]) == 200

    async def test_stamp_failure_is_soft(self):
        with _patch_conn(_FakeConn(raise_on_execute=True)):
            await op_brief.mark_complete(EVENT_ID, "claude")  # no raise


# ═════════════════════════════════════════════════════════════════════════
# cancellation
# ═════════════════════════════════════════════════════════════════════════


class TestMentions:
    def test_by_category(self):
        assert op_brief.mentions("the interview got cancelled", _commitment())

    def test_by_content_word(self):
        c = _commitment(what="your sister's wedding")
        assert op_brief.mentions("they called off the wedding", c)

    def test_unrelated(self):
        assert not op_brief.mentions("my flight got cancelled", _commitment())


class TestCancelMentioned:
    async def test_plain_message_never_touches_db(self):
        with patch.object(op_brief, "open_events", AsyncMock()) as oe:
            assert await op_brief.cancel_mentioned("interview went great", "claude") == 0
        oe.assert_not_awaited()

    async def test_negated_cancellation_is_ignored(self):
        with patch.object(op_brief, "open_events", AsyncMock()) as oe:
            assert await op_brief.cancel_mentioned("the interview wasn't cancelled", "claude") == 0
        oe.assert_not_awaited()

    async def test_cancels_only_matching_events(self):
        events = [{"id": "a", "commitment": _commitment()},
                  {"id": "b", "commitment": _commitment(what="flight to Denver")}]
        with patch.object(op_brief, "open_events", AsyncMock(return_value=events)), \
             patch.object(op_brief, "mark_cancelled", AsyncMock()) as mc:
            n = await op_brief.cancel_mentioned("my interview got moved to friday", "claude")
        assert n == 1
        mc.assert_awaited_once_with("a", "claude", "my interview got moved to friday")

    async def test_no_match_cancels_nothing(self):
        with patch.object(op_brief, "open_events", AsyncMock(return_value=[
                {"id": "a", "commitment": _commitment()}])), \
             patch.object(op_brief, "mark_cancelled", AsyncMock()) as mc:
            assert await op_brief.cancel_mentioned("game got cancelled", "claude") == 0
        mc.assert_not_awaited()


# ═════════════════════════════════════════════════════════════════════════
# on_turn (planning)
# ═════════════════════════════════════════════════════════════════════════


_RAW = {"what": "job interview", "when_hint": "thursday at 9am",
        "sensitivity": "normal", "confidence": 0.9}


@pytest.fixture
def planning():
    schedule = AsyncMock(return_value="task")
    store = AsyncMock(return_value=EVENT_ID)
    cancel = AsyncMock(return_value=0)
    with patch.object(op_brief, "now_local", return_value=NOW), \
         patch.object(op_brief.deferred, "schedule", schedule), \
         patch.object(op_brief, "store_event", store), \
         patch.object(op_brief, "cancel_mentioned", cancel), \
         patch.object(op_brief, "load_config", return_value=_cfg()):
        yield SimpleNamespace(schedule=schedule, store=store, cancel=cancel)


class TestOnTurn:
    async def test_schedules_three_slots_in_local_time(self, planning):
        n = await op_brief.on_turn("interview thursday", [_RAW], user_id="claude",
                                   affection_level=3)
        assert n == 1
        planning.cancel.assert_awaited_once_with("interview thursday", "claude")
        calls = planning.schedule.await_args_list
        assert [c.args[0]["slot"] for c in calls] == ["brief", "sendoff", "debrief"]
        first = calls[0]
        assert first.args[0] == {"kind": "op_brief", "event_id": EVENT_ID, "slot": "brief",
                                 "due": "2026-10-07T20:30:00"}
        assert first.kwargs["user_id"] == "claude"
        assert first.kwargs["due_at"] == datetime(2026, 10, 7, 20, 30, tzinfo=op_brief.LOCAL_TZ)

    async def test_cancellation_runs_even_without_events(self, planning):
        assert await op_brief.on_turn("it got cancelled", [], user_id="claude",
                                      affection_level=5) == 0
        planning.cancel.assert_awaited_once()
        planning.store.assert_not_awaited()

    async def test_non_list_events(self, planning):
        assert await op_brief.on_turn("x", {"what": "x"}, user_id="claude", affection_level=5) == 0

    async def test_below_affection_gate_plans_nothing(self, planning):
        assert await op_brief.on_turn("x", [_RAW], user_id="claude", affection_level=1) == 0
        planning.store.assert_not_awaited()

    async def test_rejected_and_past_events_are_skipped(self, planning):
        past_only = {"what": "exam", "when_hint": "tonight", "confidence": 0.9}
        with patch.object(op_brief, "now_local", return_value=datetime(2026, 10, 5, 22, 0)):
            n = await op_brief.on_turn("x", [{"what": ""}, past_only], user_id="claude",
                                       affection_level=5)
        assert n == 0
        planning.store.assert_not_awaited()

    async def test_duplicate_is_not_rescheduled(self, planning):
        planning.store.return_value = None
        assert await op_brief.on_turn("x", [_RAW], user_id="claude", affection_level=5) == 0
        planning.schedule.assert_not_awaited()

    async def test_schedule_failure_is_logged_not_raised(self, planning, caplog):
        planning.schedule.return_value = None
        assert await op_brief.on_turn("x", [_RAW], user_id="claude", affection_level=5) == 1
        assert "could not be scheduled" in caplog.text

    async def test_caps_events_per_turn(self, planning):
        raws = [dict(_RAW, what=f"exam {i}", when_hint=f"in {i + 2} days") for i in range(5)]
        assert await op_brief.on_turn("x", raws, user_id="claude", affection_level=5) == 3

    async def test_never_raises(self, planning):
        planning.cancel.side_effect = RuntimeError("boom")
        assert await op_brief.on_turn("x", [_RAW], user_id="claude", affection_level=5) == 0


# ═════════════════════════════════════════════════════════════════════════
# config + rendering
# ═════════════════════════════════════════════════════════════════════════


class TestLoadConfig:
    def test_real_yaml_section_loads(self):
        cfg = op_brief.load_config()
        assert cfg.min_affection == 2 and cfg.slip_from == 5
        assert set(cfg.pools) == {"normal", "medical", "sensitive"}
        assert all(set(cfg.pools[v]) == {"brief", "sendoff", "debrief"} for v in cfg.pools)
        assert 7 in cfg.pools["normal"]["brief"]

    def test_loader_failure_uses_defaults(self):
        with patch("app.personality.load_personality", side_effect=OSError("gone")):
            cfg = op_brief.load_config()
        assert cfg.min_affection == op_brief.DEFAULT_MIN_AFFECTION
        assert cfg.pools["normal"]["debrief"] == {0: ["Report."]}

    def test_malformed_section_falls_back_per_field(self):
        section = {
            "min_affection": "high",
            "slip_from_affection": 6,
            "slip_lines": "not a list",
            "normal": "nope",
            "medical": {"brief": {"x": ["bad key"], 3: [], 4: ["  ", 7, "Good line."]},
                        "sendoff": ["not", "a", "dict"]},
        }
        with patch("app.personality.load_personality",
                   return_value={"operation_brief": section}):
            cfg = op_brief.load_config()
        assert cfg.min_affection == op_brief.DEFAULT_MIN_AFFECTION
        assert cfg.slip_from == 6
        assert cfg.slip_lines == op_brief._DEFAULT_SLIP
        assert cfg.pools["normal"]["brief"] == {0: op_brief._DEFAULT_POOLS["normal"]["brief"]}
        assert cfg.pools["medical"]["brief"] == {4: ["Good line."]}
        assert cfg.pools["medical"]["sendoff"] == {0: op_brief._DEFAULT_POOLS["medical"]["sendoff"]}

    def test_section_not_a_dict(self):
        with patch("app.personality.load_personality", return_value={"operation_brief": []}):
            assert op_brief.load_config().slip_from == op_brief.DEFAULT_SLIP_FROM


def _cfg_with(variant: str, slot: str, pool: dict) -> op_brief.BriefConfig:
    cfg = _cfg()
    pools = {v: dict(s) for v, s in cfg.pools.items()}
    pools[variant][slot] = pool
    return op_brief.BriefConfig(cfg.min_affection, cfg.slip_from, cfg.slip_lines, pools)


class TestRender:
    def test_brief_is_opord_with_slots_filled(self):
        text = op_brief.render("brief", _commitment(), 2, _cfg())
        assert text.startswith("Briefing for tomorrow")
        assert "Your job interview at the bank, tomorrow at 0900" not in text  # mid-line stays lower
        assert "your job interview at the bank, tomorrow at 0900." in text
        assert "Eat breakfast. That's not a suggestion." in text
        assert "Come back and tell me" not in text  # no slip below 5

    def test_slip_and_cover_from_level_five(self):
        text = op_brief.render("brief", _commitment(), 5, _cfg())
        assert text.endswith("...Come back and tell me. That's not a request either.")

    def test_untimed_event_has_no_time_and_capitalizes(self):
        c = _commitment(event_time=None, time_exact=False)
        cfg = _cfg_with("normal", "sendoff", {0: ["{the_what} today{when}. Go."]})
        assert op_brief.render("sendoff", c, 3, cfg) == "The job interview at the bank today. Go."

    def test_approximate_time_is_not_shown(self):
        c = _commitment(event_time="09:00", time_exact=False)
        assert "0900" not in op_brief.render("brief", c, 2, _cfg())

    def test_possessive_what_is_not_doubled(self):
        c = _commitment(what="your sister's wedding")
        cfg = _cfg_with("normal", "debrief", {0: ["{the_what} / {your_what} / {what}"]})
        assert op_brief.render("debrief", c, 3, cfg) == \
            "Your sister's wedding / your sister's wedding / your sister's wedding"

    def test_unknown_slot_name_renders_empty(self):
        cfg = _cfg_with("normal", "debrief", {0: ["Report{nope}."]})
        assert op_brief.render("debrief", _commitment(), 3, cfg) == "Report."

    @pytest.mark.parametrize("bad", ["Broken {", "Index {0}", "Attr {what.nope}"])
    def test_malformed_template_falls_back_to_default(self, bad):
        cfg = _cfg_with("normal", "debrief", {0: [bad]})
        assert op_brief.render("debrief", _commitment(), 3, cfg) == "Report."

    def test_level_below_every_band_uses_lowest(self):
        cfg = _cfg_with("normal", "debrief", {4: ["Four."], 6: ["Six."]})
        assert op_brief.render("debrief", _commitment(), 2, cfg) == "Four."

    def test_unknown_variant_is_normal(self):
        assert op_brief.render("debrief", _commitment(variant="weird"), 2, _cfg()) == "Report."

    def test_sensitive_never_briefs_or_demands_a_report(self):
        cfg = op_brief.load_config()  # the authored YAML copy
        c = _commitment(what="your grandmother's funeral", variant="sensitive")
        for level in range(2, 10):
            for slot in op_brief.SLOTS:
                text = op_brief.render(slot, c, level, cfg)
                assert "Report" not in text and "SITUATION" not in text
                assert "Come back and tell me" not in text
                assert "here" in text  # presence, every time

    def test_medical_never_prescribes_food_or_sleep(self):
        cfg = op_brief.load_config()
        c = _commitment(what="dentist appointment", variant="medical")
        for level in range(2, 10):
            for slot in op_brief.SLOTS:
                text = op_brief.render(slot, c, level, cfg).lower()
                assert "breakfast" not in text and "eat" not in text.split()
                assert "sleep" not in text

    def test_authored_brief_stays_short(self):
        cfg = op_brief.load_config()
        for variant in op_brief.VARIANTS:
            for band, lines in cfg.pools[variant]["brief"].items():
                for line in lines:
                    words = line.format_map(op_brief._slot_values(_commitment())).split()
                    assert len(words) <= 95, (variant, band, line)


# ═════════════════════════════════════════════════════════════════════════
# delivery
# ═════════════════════════════════════════════════════════════════════════


def _action(slot="brief", due="2026-10-07T20:30:00", **extra) -> dict:
    return {"kind": "op_brief", "event_id": EVENT_ID, "slot": slot, "due": due, **extra}


class TestHoldDeadline:
    def test_brief_capped_by_evening_cutoff(self):
        assert op_brief.hold_deadline(_action(), _commitment()) == datetime(2026, 10, 7, 22, 45)

    def test_capped_by_hold_max(self):
        a = _action(slot="debrief", due="2026-10-08T10:00:00")
        assert op_brief.hold_deadline(a, _commitment()) == datetime(2026, 10, 8, 13, 0)

    def test_sendoff_never_after_the_event(self):
        a = _action(slot="sendoff", due="2026-10-08T08:05:00")
        assert op_brief.hold_deadline(a, _commitment()) == datetime(2026, 10, 8, 8, 45)

    def test_untimed_sendoff(self):
        a = _action(slot="sendoff", due="2026-10-08T08:05:00")
        c = _commitment(event_time=None, time_exact=False)
        assert op_brief.hold_deadline(a, c) == datetime(2026, 10, 8, 11, 5)


@pytest.fixture
def ctx():
    affection = MagicMock()
    affection.get_state = AsyncMock(return_value=SimpleNamespace(level=5))
    proactive = MagicMock()
    proactive._can_send = MagicMock(return_value=True)
    proactive._proactive_count_today = 0
    proactive._last_proactive_answered = True
    router = MagicMock()
    router.is_game_active = AsyncMock(return_value=False)
    ws = MagicMock()
    ws.send_proactive = AsyncMock()
    ws.is_connected = MagicMock(return_value=True)
    load = AsyncMock(return_value=_commitment())
    complete = AsyncMock()
    schedule = AsyncMock(return_value="task")
    push = AsyncMock(return_value=1)
    with patch("app.context.affection", affection), \
         patch("app.context.proactive", proactive), \
         patch("app.context.router", router), \
         patch("app.context.ws", ws), \
         patch("app.push.send_push", push), \
         patch.object(op_brief, "load_event", load), \
         patch.object(op_brief, "mark_complete", complete), \
         patch.object(op_brief.deferred, "schedule", schedule), \
         patch.object(op_brief, "load_config", return_value=_cfg()), \
         patch.object(op_brief, "now_local", return_value=datetime(2026, 10, 7, 20, 30)):
        yield SimpleNamespace(affection=affection, proactive=proactive, router=router,
                              ws=ws, load=load, complete=complete, schedule=schedule,
                              push=push)


class TestDeliver:
    async def test_sends_brief_and_counts_it(self, ctx):
        await op_brief.deliver("claude", _action())
        ctx.load.assert_awaited_once_with(EVENT_ID, "claude")
        ctx.proactive._can_send.assert_called_once_with(ignore_unanswered=True)
        user, text = ctx.ws.send_proactive.await_args.args
        assert user == "claude" and "SITUATION" in text
        assert ctx.proactive._proactive_count_today == 1
        assert ctx.proactive._last_proactive_answered is False
        ctx.push.assert_not_awaited()
        ctx.complete.assert_not_awaited()  # only the debrief closes the chain

    async def test_debrief_closes_the_chain(self, ctx):
        await op_brief.deliver("claude", _action(slot="debrief", due="2026-10-08T19:00:00"))
        assert ctx.ws.send_proactive.await_args.args[1].startswith("Report")
        ctx.complete.assert_awaited_once_with(EVENT_ID, "claude")

    async def test_offline_commander_also_gets_a_push(self, ctx):
        ctx.ws.is_connected.return_value = False
        await op_brief.deliver("claude", _action(slot="sendoff", due="2026-10-08T08:05:00"))
        ctx.ws.send_proactive.assert_awaited_once()  # still persisted to history
        ctx.push.assert_awaited_once()
        assert ctx.push.await_args.kwargs["user_id"] == "claude"

    async def test_bookkeeping_failure_after_send_is_swallowed(self, ctx):
        ctx.ws.is_connected.side_effect = RuntimeError("socket table gone")
        await op_brief.deliver("claude", _action(slot="debrief", due="2026-10-08T19:00:00"))
        ctx.ws.send_proactive.assert_awaited_once()
        ctx.complete.assert_awaited_once()

    @pytest.mark.parametrize("action", [
        {"kind": "op_brief"},
        {"kind": "op_brief", "event_id": EVENT_ID, "slot": "nope", "due": "x"},
        {"kind": "op_brief", "event_id": EVENT_ID, "slot": "brief"},
    ])
    async def test_malformed_action_is_ignored(self, ctx, action):
        await op_brief.deliver("claude", action)
        ctx.load.assert_not_awaited()
        ctx.ws.send_proactive.assert_not_awaited()

    async def test_cancelled_event_is_skipped(self, ctx):
        ctx.load.return_value = None
        await op_brief.deliver("claude", _action())
        ctx.ws.send_proactive.assert_not_awaited()

    async def test_below_affection_gate_drops_and_closes_debrief(self, ctx):
        ctx.affection.get_state.return_value = SimpleNamespace(level=1)
        await op_brief.deliver("claude", _action(slot="debrief", due="2026-10-08T19:00:00"))
        ctx.ws.send_proactive.assert_not_awaited()
        ctx.complete.assert_awaited_once()

    async def test_mid_game_is_held_and_retried(self, ctx):
        ctx.router.is_game_active.return_value = True
        await op_brief.deliver("claude", _action())
        ctx.ws.send_proactive.assert_not_awaited()
        ctx.schedule.assert_awaited_once_with(_action(), user_id="claude",
                                              delay_seconds=op_brief.HOLD_STEP_SECONDS)

    async def test_game_past_hold_window_drops(self, ctx):
        ctx.router.is_game_active.return_value = True
        with patch.object(op_brief, "now_local", return_value=datetime(2026, 10, 7, 23, 0)):
            await op_brief.deliver("claude", _action())
        ctx.schedule.assert_not_awaited()
        ctx.ws.send_proactive.assert_not_awaited()

    async def test_game_probe_failure_counts_as_not_gaming(self, ctx):
        ctx.router.is_game_active.side_effect = RuntimeError("gateway down")
        await op_brief.deliver("claude", _action())
        ctx.ws.send_proactive.assert_awaited_once()

    async def test_proactive_guard_suppresses(self, ctx):
        ctx.proactive._can_send.return_value = False  # muted / quiet hours / cap
        await op_brief.deliver("claude", _action(slot="debrief", due="2026-10-08T19:00:00"))
        ctx.ws.send_proactive.assert_not_awaited()
        ctx.complete.assert_awaited_once()

    async def test_pre_send_failure_propagates_for_retry(self, ctx):
        ctx.affection.get_state.side_effect = RuntimeError("db down")
        with pytest.raises(RuntimeError):
            await op_brief.deliver("claude", _action())
        ctx.ws.send_proactive.assert_not_awaited()
