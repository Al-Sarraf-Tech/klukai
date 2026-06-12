"""End-to-end WIRING tests for the four Phase-2 proactive/continuity features.

These tests deliberately exercise the *full dispatch path* of each feature
rather than its helper functions in isolation. The goal is to prove the
features are actually WIRED into the engine/background/handler — i.e. that a
message reaches a real captured callback (or ``ws.send_proactive``) and that
the engine's guards/flags behave end-to-end.

Everything runs WITHOUT a live stack: the LLM, DB pool, ``memory_archive`` and
``ws`` are all mocked, and clocks are frozen via ``datetime.now`` patches.

Coverage map:
  1. Scheduler wiring        — the 3 NEW jobs register on ``start()``.
  2a Memory recall dispatch  — archive+LLM -> captured callback; once/day flag.
  2d Quiet-day + seasonal    — pattern/date match -> captured callback.
  2c Level-9 oath            — bg level-up-to-9 -> oath scene once, guarded.
  2b Continuity              — ``_maybe_reflect_on_return`` prompt has the
                              missed-you beat + prior mood + last-thread ref.
  6  Daily-reset integration — every new delivered-today flag clears.

Mocking style mirrors tests/test_proactive_memory_recall.py,
tests/test_proactive_smart.py, tests/test_inside_jokes_growth.py, and
tests/test_emotional_continuity.py.
"""

from __future__ import annotations

import contextlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("psycopg")

import app.background as bg
from app.proactive import ProactiveEngine

# ── Frozen clocks ─────────────────────────────────────────────────────────────
# 2026-05-17 is a Sunday. 15:00 is inside the allowed send window (quiet hours
# are 23:00–08:00) and past the early-afternoon pattern-check time, so every
# guard (_can_send, weekday match) lines up for a real dispatch.
_AFTERNOON_SUNDAY = datetime(2026, 5, 17, 15, 0, 0)

# datetime.now() is bound separately inside each proactive submodule; freeze all
# the ones the dispatch paths in this file actually touch.
_DATETIME_TARGETS = (
    "app.proactive.engine.now_local",
    "app.proactive.events.now_local",
    "app.proactive.patterns.now_local",
)


@contextlib.contextmanager
def _patch_now(value: datetime = _AFTERNOON_SUNDAY):
    """Freeze datetime.now() across the proactive submodules that bind it."""
    mock_dt = MagicMock(return_value=value)
    with contextlib.ExitStack() as stack:
        for target in _DATETIME_TARGETS:
            stack.enter_context(patch(target, mock_dt))
        yield mock_dt


def _gate():
    """An async-context-manager stand-in for the LM concurrency gate."""
    gate = AsyncMock()
    gate.__aenter__ = AsyncMock()
    gate.__aexit__ = AsyncMock()
    return gate


def _ready_engine(affection: int = 5) -> ProactiveEngine:
    """An engine in a clean, send-ready state with a *real* captured callback.

    The callback is a plain AsyncMock that records every message handed to it —
    this is what lets these tests assert the message went all the way through the
    delivery path, not just that a helper returned a string.
    """
    e = ProactiveEngine()
    e._affection_level = affection
    e._last_mood = "tender"
    e._muted_until = None
    e._last_proactive_answered = True
    e._proactive_count_today = 0
    e._on_message_callback = AsyncMock()
    return e


# ═══════════════════════════════════════════════════════════════════════════
# 1. Scheduler wiring — the three NEW jobs register on start()
# ═══════════════════════════════════════════════════════════════════════════


class TestSchedulerWiringE2E:
    """``start()`` must register the new jobs alongside the pre-existing ones.

    We patch the engine's scheduler with a mock so no real APScheduler threads
    spin up, then assert on the ``add_job`` ids — proving the wiring without a
    running loop or background timers.
    """

    @pytest.mark.asyncio
    async def test_start_registers_new_and_existing_jobs(self):
        e = ProactiveEngine()
        fake_scheduler = MagicMock()
        e._scheduler = fake_scheduler

        e.start()

        # Every add_job(...) call carries an id= kwarg; collect them all.
        registered_ids = {
            call.kwargs["id"]
            for call in fake_scheduler.add_job.call_args_list
            if "id" in call.kwargs
        }

        # The 3 NEW Phase-2 jobs must be present...
        for new_id in ("memory_recall", "seasonal_check", "quiet_day_check"):
            assert new_id in registered_ids, f"missing new job: {new_id}"

        # ...without dropping the pre-existing schedule.
        for existing_id in (
            "morning_checkin", "evening_checkin", "daily_reset",
            "romance_window", "anniversary_check",
        ):
            assert existing_id in registered_ids, f"regressed existing job: {existing_id}"

        # The scheduler was actually started (the engine doesn't just build jobs).
        fake_scheduler.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_new_jobs_bound_to_correct_handlers(self):
        """Each new job id is wired to the matching coroutine, not a stub."""
        e = ProactiveEngine()
        fake_scheduler = MagicMock()
        e._scheduler = fake_scheduler
        e.start()

        bound = {
            call.kwargs["id"]: call.args[0]
            for call in fake_scheduler.add_job.call_args_list
            if "id" in call.kwargs and call.args
        }
        assert bound["memory_recall"] == e._memory_recall_tick
        assert bound["seasonal_check"] == e._seasonal_check
        assert bound["quiet_day_check"] == e._quiet_day_check


# ═══════════════════════════════════════════════════════════════════════════
# 2a. Memory recall dispatch — archive + LLM -> captured callback
# ═══════════════════════════════════════════════════════════════════════════

_FAKE_MEMORY = {"annotation": "The night on the observation deck, watching the rain."}
_RECALL_TEXT = "Remember the rain on the deck, Commander? I still keep that one close."


class TestMemoryRecallDispatchE2E:
    @pytest.mark.asyncio
    async def test_full_path_delivers_llm_text_through_callback(self):
        """affection>=4 + real archive memory + LLM text -> the *captured*
        callback receives exactly that text, and the once/day + count/answered
        bookkeeping all flips."""
        e = _ready_engine(affection=4)
        gate = _gate()
        with _patch_now(), \
             patch("app.memory_archive.recall_memory",
                   new=AsyncMock(return_value=_FAKE_MEMORY)), \
             patch("app.llm_json.call_llm_text",
                   new=AsyncMock(return_value=_RECALL_TEXT)), \
             patch("app.llm_router.get_lm_gate", return_value=gate):
            await e._memory_recall_event()

        e._on_message_callback.assert_awaited_once_with(_RECALL_TEXT)
        assert e._memory_recall_delivered_today is True
        assert e._proactive_count_today == 1
        assert e._last_proactive_answered is False

    @pytest.mark.asyncio
    async def test_scheduled_tick_drives_the_event_end_to_end(self):
        """The scheduled gate (_memory_recall_tick) on a passing roll runs the
        real event, which then dispatches through the callback — proving the
        cron entry point is wired to the delivery path, not just the helper."""
        e = _ready_engine(affection=5)
        gate = _gate()
        with _patch_now(), \
             patch("app.proactive.events.random.random", return_value=0.10), \
             patch("app.memory_archive.recall_memory",
                   new=AsyncMock(return_value=_FAKE_MEMORY)), \
             patch("app.llm_json.call_llm_text",
                   new=AsyncMock(return_value=_RECALL_TEXT)), \
             patch("app.llm_router.get_lm_gate", return_value=gate):
            await e._memory_recall_tick()

        e._on_message_callback.assert_awaited_once_with(_RECALL_TEXT)
        assert e._memory_recall_delivered_today is True

    @pytest.mark.asyncio
    async def test_once_per_day_flag_blocks_second_send_then_reset_reopens(self):
        """After one delivery the flag suppresses a second send; _reset_daily
        clears it so the next day can fire again."""
        e = _ready_engine(affection=5)
        gate = _gate()
        recall = AsyncMock(return_value=_FAKE_MEMORY)
        with _patch_now(), \
             patch("app.memory_archive.recall_memory", new=recall), \
             patch("app.llm_json.call_llm_text",
                   new=AsyncMock(return_value=_RECALL_TEXT)), \
             patch("app.llm_router.get_lm_gate", return_value=gate):
            await e._memory_recall_event()   # 1st: delivers
            await e._memory_recall_event()   # 2nd: blocked by once/day flag

        assert e._on_message_callback.await_count == 1
        assert recall.await_count == 1  # gated out before touching the archive

        # Daily reset re-opens the gate.
        await e._reset_daily()
        assert e._memory_recall_delivered_today is False


# ═══════════════════════════════════════════════════════════════════════════
# 2d. Quiet-day + seasonal dispatch
# ═══════════════════════════════════════════════════════════════════════════


def _quiet_sunday_pattern_cache():
    """Pre-seed the pattern cache so no DB is needed; today (frozen) is Sunday
    (dow=0 in the Sun=0 indexing the pattern dict uses)."""
    return {
        "patterns:jalsarraf": (
            _AFTERNOON_SUNDAY,
            {
                "quiet_on_sunday": {
                    "type": "quiet_day", "day": "sunday", "dow": 0,
                    "confidence": 0.95, "user_msgs": 0, "overall_avg": 10.0,
                },
            },
        ),
    }


_SEASONAL_CFG = {
    "seasonal_events": {
        "christmas": {
            "month": 12, "day": 25, "min_affection": 0,
            "messages": ["Merry Christmas, Commander. ...Stay warm."],
        },
    }
}


class TestQuietDayAndSeasonalDispatchE2E:
    @pytest.mark.asyncio
    async def test_quiet_day_pattern_delivers_checkin_through_callback(self):
        """A strong quiet-day pattern matching today's weekday flows all the way
        to the captured callback (with the day name interpolated) and trips the
        delivery bookkeeping."""
        e = _ready_engine(affection=3)
        e._pattern_cache = _quiet_sunday_pattern_cache()
        with _patch_now(), \
             patch("app.proactive.events.publish_event", new=AsyncMock()):
            await e._quiet_day_check("jalsarraf")

        e._on_message_callback.assert_awaited_once()
        delivered = e._on_message_callback.call_args.args[0]
        assert "Sunday" in delivered  # {day} interpolated from the matched pattern
        assert e._quiet_day_delivered_today is True
        assert e._proactive_count_today == 1
        assert e._last_proactive_answered is False

    @pytest.mark.asyncio
    async def test_quiet_day_runs_off_real_pattern_query_not_just_cache(self):
        """End-to-end through detect_activity_patterns: a mocked DB returns a
        quiet-Sunday history, the profiler derives the pattern, and the check-in
        dispatches — proving the pattern->events wiring, not a hand-seeded cache.
        """
        e = _ready_engine(affection=3)
        # A history where every weekday is busy EXCEPT Sunday (dow=0).
        rows = [(d, 40, 4) for d in (1, 2, 3, 4, 5, 6)]  # Mon..Sat, Sunday absent
        res = AsyncMock()
        res.fetchall = AsyncMock(return_value=rows)
        conn = MagicMock()
        conn.execute = AsyncMock(return_value=res)
        db_ctx = AsyncMock()
        db_ctx.__aenter__ = AsyncMock(return_value=conn)
        db_ctx.__aexit__ = AsyncMock(return_value=False)

        with _patch_now(), \
             patch("app.db.get_conn", return_value=db_ctx), \
             patch("app.proactive.events.publish_event", new=AsyncMock()):
            await e._quiet_day_check("jalsarraf")

        e._on_message_callback.assert_awaited_once()
        assert "Sunday" in e._on_message_callback.call_args.args[0]

    @pytest.mark.asyncio
    async def test_seasonal_date_match_delivers_greeting_through_callback(self):
        """On a matching seasonal date the greeting is dispatched and the
        per-occurrence guard key is recorded."""
        e = _ready_engine(affection=5)
        christmas = datetime(2026, 12, 25, 9, 0, 0)
        with patch("app.proactive.events.now_local") as mock_dt, \
             patch("app.personality.load_personality", return_value=_SEASONAL_CFG), \
             patch("app.proactive.events.publish_event", new=AsyncMock()):
            mock_dt.return_value = christmas
            await e._seasonal_check()

        e._on_message_callback.assert_awaited_once()
        assert "Christmas" in e._on_message_callback.call_args.args[0]
        assert e._seasonal_delivered.get("christmas:2026-12-25") is True

    @pytest.mark.asyncio
    async def test_seasonal_no_dispatch_on_non_matching_date(self):
        """A non-holiday date must not dispatch anything."""
        e = _ready_engine(affection=5)
        with patch("app.proactive.events.now_local") as mock_dt, \
             patch("app.personality.load_personality", return_value=_SEASONAL_CFG):
            mock_dt.return_value = datetime(2026, 7, 4, 9, 0, 0)  # July 4
            await e._seasonal_check()
        e._on_message_callback.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# 2c. Level-9 oath capstone — via the real background.py level-up path
# ═══════════════════════════════════════════════════════════════════════════

_OATH_SCENE = [
    "...Commander. Stay.",
    "Indigo gown. I chose you. Every day, I choose you again. The oath is fulfilled.",
    "...Thank you. For answering.",
]


def _aff_change(*, new_level=9, level_changed=True, level_direction="up"):
    return SimpleNamespace(
        new_score=820, new_level=new_level, new_level_name="Devoted Oath",
        delta=10, level_changed=level_changed, level_direction=level_direction,
    )


def _session(turn_count: int = 1):
    turns = [{"role": "user", "content": f"Turn {i}"} for i in range(turn_count)]
    return SimpleNamespace(
        conversation_id="conv-1", turns=turns, context_summary=None,
        mood="composed", turn_count=turn_count,
        mission_description=None, mission_interval=None, mission_started_at=None,
    )


def _base_extract_result(**overrides) -> dict:
    result = {
        "facts": [], "mood": "composed", "topics": [], "should_remember": False,
        "interaction": {"type": "neutral", "intensity": 5},
        "commander_details": {}, "gift_item": None, "inside_joke": None,
    }
    result.update(overrides)
    return result


@pytest.fixture
def bg_mocks(monkeypatch):
    """Patch every collaborator background_extraction touches, tuned for the
    level-9 oath path (mirrors tests/test_inside_jokes_growth.py::bg_mocks).

    record_first defaults to True (first ever) so the oath fires unless a test
    overrides it; flip it to False to simulate the firsts-guard being set.
    """
    monkeypatch.setattr(bg.asyncio, "sleep", AsyncMock())

    affection = MagicMock()
    affection.get_state = AsyncMock(return_value=SimpleNamespace(level=8, score=800))
    affection.apply_classification = AsyncMock(return_value=_aff_change())

    memory = MagicMock()
    memory.set_relationship_fact = AsyncMock()
    memory.set_inside_joke = AsyncMock()
    memory.save_session = AsyncMock()
    memory.store_exchange = AsyncMock()
    memory.store_episode = AsyncMock()
    memory.record_milestone = AsyncMock(return_value=True)
    memory.get_session = AsyncMock(return_value=_session(2))

    ws = MagicMock()
    for name in ("send", "send_mood", "send_proactive", "send_thinking",
                 "send_affection", "send_affection_level_change",
                 "send_heartbeat_spike"):
        setattr(ws, name, AsyncMock())

    proactive = MagicMock()
    proactive.store_gift = AsyncMock()
    proactive.record_first = AsyncMock(return_value=True)
    proactive.set_last_mood = MagicMock()
    proactive.set_affection_level = MagicMock()
    proactive.start_mission = MagicMock()
    proactive.mission_active = False
    proactive._last_mood = "composed"

    context_mod = MagicMock()
    context_mod.COMPACT_THRESHOLD = 8
    context_mod.COMPACT_KEEP_RAW = 4
    context_mod.get_last_memory_id = MagicMock(return_value=None)
    context_mod.set_last_memory_id = MagicMock()

    memory_archive = MagicMock()
    memory_archive.update_curation = AsyncMock()
    memory_archive.save_image = AsyncMock(return_value="mem-1")

    extract_facts = AsyncMock(return_value=_base_extract_result())

    monkeypatch.setattr(bg, "affection", affection)
    monkeypatch.setattr(bg, "memory", memory)
    monkeypatch.setattr(bg, "ws", ws)
    monkeypatch.setattr(bg, "proactive", proactive)
    monkeypatch.setattr(bg, "context", context_mod)
    monkeypatch.setattr(bg, "memory_archive", memory_archive)
    monkeypatch.setattr(bg, "extract_facts", extract_facts)
    monkeypatch.setattr(bg, "session_id", lambda uid: f"session:{uid}")
    monkeypatch.setattr(bg, "load_personality", MagicMock(return_value={
        "affection": {
            "oath_fulfilled_scene": list(_OATH_SCENE),
            "level_up_messages": {9: "Every day, I choose you again."},
            "milestone_scenes": {9: ["generic lv9 line"]},
        }
    }))

    return SimpleNamespace(
        affection=affection, memory=memory, ws=ws, proactive=proactive,
        context=context_mod, memory_archive=memory_archive,
        extract_facts=extract_facts,
    )


class TestLevelNineOathE2E:
    @pytest.mark.asyncio
    async def test_oath_scene_delivered_once_when_firsts_guard_unset(self, bg_mocks):
        """First arrival at lv9 with the firsts-guard UNSET (record_first->True)
        dispatches every oath-scene line via ws.send_proactive, and guards it
        through record_first('oath_fulfilled')."""
        await bg.background_extraction("m", "r", _session(1), user_id="u1")

        sent = [c.args[1] for c in bg_mocks.ws.send_proactive.await_args_list]
        for line in _OATH_SCENE:
            assert line in sent, f"missing oath line: {line!r}"
        bg_mocks.proactive.record_first.assert_any_await("u1", "oath_fulfilled")

    @pytest.mark.asyncio
    async def test_oath_not_redelivered_when_firsts_guard_set(self, bg_mocks):
        """With the firsts-guard SET (record_first->False) the oath scene and the
        generic milestone scene are both suppressed; only the short level-up line
        is sent — proving the once-ever guard is honored end-to-end."""
        bg_mocks.proactive.record_first.return_value = False  # guard already set
        bg_mocks.memory.record_milestone.return_value = False  # milestone seen too

        await bg.background_extraction("m", "r", _session(1), user_id="u1")

        sent = [c.args[1] for c in bg_mocks.ws.send_proactive.await_args_list]
        assert not any(line in sent for line in _OATH_SCENE)
        assert "generic lv9 line" not in sent
        assert "Every day, I choose you again." in sent


# ═══════════════════════════════════════════════════════════════════════════
# 2b. Continuity — _maybe_reflect_on_return prompt content
# ═══════════════════════════════════════════════════════════════════════════


class _FakeConn:
    """Connection returning queued batches in query order.

    Tuples -> fetchone (single row); lists -> fetchall (multiple rows).
    Mirrors tests/test_emotional_continuity.py.
    """

    def __init__(self, *batches):
        self._batches = list(batches)

    async def execute(self, sql, params=None):
        result = AsyncMock()
        if self._batches:
            batch = self._batches.pop(0)
            if isinstance(batch, tuple):
                result.fetchone = AsyncMock(return_value=batch)
            else:
                result.fetchall = AsyncMock(return_value=batch)
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _FakePool:
    def __init__(self, *batch_sequences):
        self._sequences = list(batch_sequences)

    def connection(self):
        seq = self._sequences.pop(0) if self._sequences else []
        return _FakeConn(*seq)


def _capture_router():
    """Router whose complete_local records the prompt it received."""
    captured: dict[str, str] = {}

    async def _complete(system_prompt, messages, config):
        captured["system"] = system_prompt
        captured["user"] = messages[0]["content"]
        return {"choices": [{"message": {"content":
                "Welcome back, Commander. You were telling me about the recon route."}}]}

    router = MagicMock()
    router.complete_local = AsyncMock(side_effect=_complete)
    return router, captured


class TestContinuityReflectionE2E:
    @pytest.mark.asyncio
    async def test_return_prompt_carries_missed_you_mood_and_last_thread(self):
        """A multi-day gap + a prior mood + recent excerpts -> the prompt handed
        to the LLM contains the 'missed you' beat AND the carried-over mood AND a
        reference to the last conversational thread; the greeting then dispatches
        via ws.send_proactive."""
        from app.chat import _maybe_reflect_on_return

        hours = 60  # ~2.5 days: inside the 8-72h return window, openly missed
        away = datetime.now(timezone.utc) - timedelta(hours=hours)
        excerpts = [
            ("user", "I was telling you about the recon route through Sector 7"),
            ("assistant", "go on, I'm listening"),
        ]
        # Three queries in one connection: MAX(created_at), recent rows, mood.
        pool = _FakePool([(away,), excerpts, ("yearning",)])

        router, captured = _capture_router()

        fake_ws = MagicMock()
        fake_ws.is_connected = MagicMock(return_value=True)
        fake_ws.send_proactive = AsyncMock()

        fake_aff = MagicMock()
        fake_aff.get_state = AsyncMock(
            return_value=SimpleNamespace(level=8, level_name="Devoted", score=800)
        )

        with patch("app.db.get_pool", return_value=pool), \
             patch("app.reflect_helpers.router", router), \
             patch("app.reflect_helpers.ws", fake_ws), \
             patch("app.reflect_helpers.affection", fake_aff), \
             patch("app.personality.load_personality",
                   return_value={"user_title": "Commander"}), \
             patch("app.personality.build_character_preamble",
                   return_value="You are Klukai."), \
             patch("asyncio.sleep", new=AsyncMock(return_value=None)):
            await _maybe_reflect_on_return("alice")

        # The greeting actually went out through the wired delivery path.
        fake_ws.send_proactive.assert_awaited_once()

        prompt = captured["user"]
        lowered = prompt.lower()
        # (1) missed-you beat — high closeness + multi-day gap.
        assert "how the time apart felt to you" in lowered
        assert "miss" in lowered
        # (2) prior mood carried into the new session.
        assert "yearning" in prompt
        # (3) last-thread reference so she can resume the conversation.
        assert "recon route" in prompt
        assert "resume from here" in lowered

    @pytest.mark.asyncio
    async def test_brand_new_user_does_not_dispatch(self):
        """No prior messages (MAX(created_at) is NULL) -> silent, no LLM, no WS.
        Confirms the guard short-circuits before the delivery path."""
        from app.chat import _maybe_reflect_on_return

        pool = _FakePool([(None,)])  # MAX(created_at) -> NULL
        router, captured = _capture_router()
        fake_ws = MagicMock()
        fake_ws.is_connected = MagicMock(return_value=True)
        fake_ws.send_proactive = AsyncMock()
        fake_aff = MagicMock()
        fake_aff.get_state = AsyncMock(
            return_value=SimpleNamespace(level=8, level_name="Devoted", score=800)
        )

        with patch("app.db.get_pool", return_value=pool), \
             patch("app.reflect_helpers.router", router), \
             patch("app.reflect_helpers.ws", fake_ws), \
             patch("app.reflect_helpers.affection", fake_aff), \
             patch("asyncio.sleep", new=AsyncMock(return_value=None)):
            await _maybe_reflect_on_return("brand-new")

        router.complete_local.assert_not_awaited()
        fake_ws.send_proactive.assert_not_awaited()
        assert captured == {}


# ═══════════════════════════════════════════════════════════════════════════
# 6. Daily-reset integration — every new delivered-today flag clears
# ═══════════════════════════════════════════════════════════════════════════


class TestDailyResetIntegrationE2E:
    @pytest.mark.asyncio
    async def test_reset_clears_all_phase2_delivery_flags(self):
        """Set every new delivered-today flag, run _reset_daily, assert all
        cleared — so each feature can fire again the next day."""
        e = ProactiveEngine()
        e._memory_recall_delivered_today = True
        e._quiet_day_delivered_today = True
        e._seasonal_delivered = {"christmas:2026-12-25": True}
        # Pre-existing daily flags should also reset (full integration).
        e._romance_delivered_today = True
        e._dream_delivered_today = True
        e._proactive_count_today = 7
        e._user_messaged_today = True

        await e._reset_daily()

        assert e._memory_recall_delivered_today is False
        assert e._quiet_day_delivered_today is False
        assert e._seasonal_delivered == {}
        assert e._romance_delivered_today is False
        assert e._dream_delivered_today is False
        assert e._proactive_count_today == 0
        assert e._user_messaged_today is False
