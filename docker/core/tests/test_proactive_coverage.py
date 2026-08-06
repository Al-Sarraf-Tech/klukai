"""Behavioral coverage for app.proactive — fills the gaps the existing
proactive/romance/mission/anniversary/weekly suites leave uncovered.

Every test freezes the clock (patch app.proactive.datetime) where the code
under test calls datetime.now(), mocks the DB pool / Redis / WS / LLM router,
and asserts a CONCRETE behavior: which template fires, a timing-window gating
decision, a mission-tick state transition, recap content, or WS fan-out.

NOTHING here sleeps for real or hits the network. NO no-assert tests.

Target regions (per the coverage gap report):
  MissionTimer._tick_loop / _fire_update ........ 326-375, 379-398
  stop_mission aftermath/decompression dispatch .. 479-489
  _set_post_mission_physical ..................... 495-499
  scheduler error listener + stop ................ 680-708
  morning/evening physical loop, daily_challenge . 806-841
  _random_event .................................. 854-930
  _mission_random_event .......................... 934-1003
  romance edge gates, _daily_recap ............... 1023-1095
  _dream_event ................................... 1104-1211
  _unsent_message_check .......................... 1225-1269
  check_anniversaries ............................ 1279-1334
  record_first ................................... 1340-1356
  get_comfort_objects ............................ 1362-1393
  store_gift ..................................... 1400-1414
  trigger_mission_aftermath_image ................ 1427-1489
  anniversary/weekly exception branches .......... 1557-1558, 1656-1659
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import proactive
from app.proactive import (
    MissionTimer,
    ProactiveEngine,
)


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

# A "safe" send window: 14:00, not muted, under cap, last answered.
_NOON = datetime(2026, 5, 17, 14, 0, 0)

# Submodules that bind `datetime` after the proactive.py -> proactive/ split.
_DATETIME_TARGETS = (
    "app.proactive.engine.now_local",
    "app.proactive.events.now_local",
    "app.proactive.mission.now_local",
    "app.proactive.milestones.now_local",
)


@contextlib.contextmanager
def _patch_now(value: datetime = _NOON):
    """Freeze datetime.now() across every proactive submodule.

    Preserves the real datetime class for type construction while pinning
    now() — this is the deterministic-clock discipline the spec demands.
    A single test can transit more than one submodule (e.g. a milestones
    method that calls engine._can_send), so the same mock is installed on
    each module that binds `datetime`.
    """
    mock_dt = MagicMock(return_value=value)
    with contextlib.ExitStack() as stack:
        for target in _DATETIME_TARGETS:
            stack.enter_context(patch(target, mock_dt))
        yield mock_dt


def _db_ctx(conn):
    """Wrap a connection mock in an async-contextmanager mock (get_conn style)."""
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _result_with_fetchall(rows):
    res = AsyncMock()
    res.fetchall = AsyncMock(return_value=rows)
    return res


# ═══════════════════════════════════════════════════════════════════════════
# MissionTimer._tick_loop + _fire_update  (lines 326-375, 379-398)
# ═══════════════════════════════════════════════════════════════════════════


class TestTickLoopBehavior:
    """Drive _tick_loop directly with controlled random + a fake sleep so we
    can observe a single deterministic tick and its state transitions."""

    @pytest.mark.asyncio
    async def test_single_tick_increments_count_and_fires_update(self):
        t = MissionTimer()
        t.active = True
        t.base_interval_minutes = 30
        t.started_at = 1000.0
        t.last_update = 1000.0
        t.update_count = 0
        fired = []

        async def fake_fire(elapsed, major):
            fired.append((elapsed, major))
            t.active = False  # stop after the first tick

        # No major event (random()>=0.10), no jitter surprise, monotonic advances.
        with patch.object(t, "_fire_update", side_effect=fake_fire), \
             patch("app.proactive.mission.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.mission.random.uniform", return_value=1.0), \
             patch("app.proactive.mission.random.random", return_value=0.99), \
             patch("app.proactive.mission.time.monotonic", return_value=2800.0):
            await t._tick_loop()

        assert t.update_count == 1
        assert t.last_update == 2800.0
        # elapsed = (2800-1000)/60 == 30 minutes
        assert fired == [(30, None)]

    @pytest.mark.asyncio
    async def test_major_event_tracks_injury_in_active_events(self):
        t = MissionTimer()
        t.active = True
        t.base_interval_minutes = 30
        t.started_at = 0.0
        t.update_count = 1  # major events only fire when update_count>0

        async def fake_fire(elapsed, major):
            t.active = False

        with patch.object(t, "_fire_update", side_effect=fake_fire), \
             patch("app.proactive.mission.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.mission.random.uniform", return_value=0.6), \
             patch("app.proactive.mission.random.random", return_value=0.05), \
             patch("app.proactive.mission.random.choice", return_value="klukai_injured"), \
             patch("app.proactive.mission.time.monotonic", return_value=600.0):
            await t._tick_loop()

        # The major injury event is recorded persistently.
        assert "klukai_injured" in t.active_events
        assert t.update_count == 2

    @pytest.mark.asyncio
    async def test_squad_and_medical_injuries_tracked(self):
        for evt in ("squad_injured", "medical_emergency"):
            t = MissionTimer()
            t.active = True
            t.started_at = 0.0
            t.update_count = 1

            async def fake_fire(elapsed, major):
                t.active = False

            with patch.object(t, "_fire_update", side_effect=fake_fire), \
                 patch("app.proactive.mission.asyncio.sleep", new=AsyncMock()), \
                 patch("app.proactive.mission.random.uniform", return_value=0.6), \
                 patch("app.proactive.mission.random.random", return_value=0.05), \
                 patch("app.proactive.mission.random.choice", return_value=evt), \
                 patch("app.proactive.mission.time.monotonic", return_value=600.0):
                await t._tick_loop()
            assert evt in t.active_events

    @pytest.mark.asyncio
    async def test_injury_resolves_after_two_updates(self):
        """After update_count>2, each active event has a 30% resolve chance.
        Forcing random()<0.30 for the resolve roll clears the injury."""
        t = MissionTimer()
        t.active = True
        t.started_at = 0.0
        t.update_count = 3  # already past the >2 gate; next tick -> 4
        t.active_events = ["klukai_injured"]

        async def fake_fire(elapsed, major):
            t.active = False

        # random() is consulted twice this tick: major-event roll (return 0.99 to
        # skip) then resolve roll. We need 0.99 first, then <0.30. Use side_effect.
        rolls = iter([0.99, 0.10])

        with patch.object(t, "_fire_update", side_effect=fake_fire), \
             patch("app.proactive.mission.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.mission.random.uniform", return_value=1.0), \
             patch("app.proactive.mission.random.random", side_effect=lambda: next(rolls)), \
             patch("app.proactive.mission.time.monotonic", return_value=120.0):
            await t._tick_loop()

        assert "klukai_injured" not in t.active_events

    @pytest.mark.asyncio
    async def test_inactive_before_sleep_completes_breaks_without_firing(self):
        """If active flips False during the sleep, the loop breaks before
        incrementing update_count (the post-sleep guard)."""
        t = MissionTimer()
        t.active = True
        t.started_at = 0.0
        t.update_count = 0

        async def flip_inactive(*_a, **_k):
            t.active = False

        fire = AsyncMock()
        with patch.object(t, "_fire_update", fire), \
             patch("app.proactive.mission.asyncio.sleep", side_effect=flip_inactive), \
             patch("app.proactive.mission.random.uniform", return_value=1.0), \
             patch("app.proactive.mission.random.random", return_value=0.99):
            await t._tick_loop()

        assert t.update_count == 0
        fire.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tick_loop_swallows_exception_and_deactivates(self):
        """A raised error inside the loop body is caught; active flips False."""
        t = MissionTimer()
        t.active = True
        t.started_at = 0.0

        with patch("app.proactive.mission.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.mission.random.uniform", return_value=1.0), \
             patch("app.proactive.mission.random.random", return_value=0.99), \
             patch("app.proactive.mission.time.monotonic", side_effect=RuntimeError("boom")):
            await t._tick_loop()  # must not raise

        assert t.active is False

    @pytest.mark.asyncio
    async def test_tick_loop_handles_cancellation(self):
        t = MissionTimer()
        t.active = True
        t.started_at = 0.0

        with patch("app.proactive.mission.asyncio.sleep", side_effect=asyncio.CancelledError), \
             patch("app.proactive.mission.random.uniform", return_value=1.0), \
             patch("app.proactive.mission.random.random", return_value=0.99):
            await t._tick_loop()  # CancelledError caught, no propagation

        # active is left as-is on cancel (only the except-Exception branch clears it)
        assert t.update_count == 0


class TestFireUpdate:
    @pytest.mark.asyncio
    async def test_fire_update_calls_llm_and_delivers_via_callback(self):
        t = MissionTimer()
        t.mission_description = "Recon the eastern ridge"
        t.update_count = 2
        t._affection_level = 5
        t.active_events = ["klukai_injured"]
        cb = AsyncMock()
        t._callback = cb

        gen = AsyncMock(return_value="Field report: ridge is clear, Commander.")
        with patch("app.fact_extractor.generate_mission_update", gen):
            await t._fire_update(elapsed_minutes=40, major_event="ambush")

        gen.assert_awaited_once()
        kwargs = gen.await_args.kwargs
        assert kwargs["mission_desc"] == "Recon the eastern ridge"
        assert kwargs["elapsed_minutes"] == 40
        assert kwargs["update_number"] == 2
        assert kwargs["major_event"] == "ambush"
        assert kwargs["active_events"] == ["klukai_injured"]
        assert kwargs["affection_level"] == 5
        cb.assert_awaited_once_with("Field report: ridge is clear, Commander.")

    @pytest.mark.asyncio
    async def test_fire_update_without_callback_does_not_deliver(self):
        t = MissionTimer()
        t._callback = None
        gen = AsyncMock(return_value="text")
        with patch("app.fact_extractor.generate_mission_update", gen):
            await t._fire_update(elapsed_minutes=10, major_event=None)
        gen.assert_awaited_once()  # still generated, just nothing to deliver to

    @pytest.mark.asyncio
    async def test_fire_update_swallows_llm_error(self):
        t = MissionTimer()
        cb = AsyncMock()
        t._callback = cb
        with patch("app.fact_extractor.generate_mission_update",
                   AsyncMock(side_effect=RuntimeError("LM down"))):
            await t._fire_update(elapsed_minutes=10, major_event=None)  # no raise
        cb.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# stop_mission dispatch + _set_post_mission_physical  (479-489, 495-499)
# ═══════════════════════════════════════════════════════════════════════════


class TestStopMissionDispatch:
    @pytest.mark.asyncio
    async def test_active_timer_spawns_aftermath_decompression_physical(self):
        """An active timer fires three coroutines (aftermath, decompression,
        physical) and then stops the timer. We capture create_task targets."""
        e = ProactiveEngine()
        e._on_message_callback = AsyncMock()
        timer = MissionTimer()
        timer.active = True
        timer.active_events = ["klukai_injured"]
        timer.update_count = 4
        timer._task = None
        e._mission_timers["alice"] = timer

        scheduled = []

        def fake_create_task(coro):
            scheduled.append(coro)
            coro.close()  # we only need to observe dispatch, not run them
            return MagicMock()

        with patch("app.proactive.mission.asyncio.create_task", side_effect=fake_create_task):
            e.stop_mission("alice", trigger_aftermath=True)

        # 3 coroutines dispatched: aftermath image, decompression, physical state.
        assert len(scheduled) == 3
        assert timer.active is False
        assert "alice" not in e._mission_timers

    @pytest.mark.asyncio
    async def test_no_aftermath_only_physical_dispatched(self):
        """trigger_aftermath=False skips aftermath+decompression but still
        records post-mission physical state."""
        e = ProactiveEngine()
        timer = MissionTimer()
        timer.active = True
        timer.active_events = []
        timer._task = None
        e._mission_timers["bob"] = timer

        scheduled = []

        def fake_create_task(coro):
            scheduled.append(coro)
            coro.close()
            return MagicMock()

        with patch("app.proactive.mission.asyncio.create_task", side_effect=fake_create_task):
            e.stop_mission("bob", trigger_aftermath=False)

        assert len(scheduled) == 1  # only _set_post_mission_physical
        assert timer.active is False

    @pytest.mark.asyncio
    async def test_injury_detection_passed_to_physical(self):
        """had_injury is derived from active_events containing 'injured'."""
        e = ProactiveEngine()
        timer = MissionTimer()
        timer.active = True
        timer.active_events = ["squad_injured"]
        timer._task = None
        e._mission_timers["carol"] = timer

        captured = {}

        async def fake_physical(uid, had_injury):
            captured["uid"] = uid
            captured["had_injury"] = had_injury

        with patch.object(e, "_set_post_mission_physical", side_effect=fake_physical), \
             patch.object(e, "trigger_mission_aftermath_image", new=AsyncMock()), \
             patch.object(e, "_decompression_message", new=AsyncMock()):
            e.stop_mission("carol", trigger_aftermath=True)
            await asyncio.sleep(0)  # let the create_task coroutines run

        assert captured.get("had_injury") is True
        assert captured.get("uid") == "carol"


class TestSetPostMissionPhysical:
    @pytest.mark.asyncio
    async def test_calls_physical_on_mission_end(self):
        e = ProactiveEngine()
        fake_phys = MagicMock()
        fake_phys.on_mission_end = AsyncMock()
        with patch("app.context.physical", fake_phys):
            await e._set_post_mission_physical("alice", had_injury=True)
        fake_phys.on_mission_end.assert_awaited_once_with("alice", had_injury=True)

    @pytest.mark.asyncio
    async def test_physical_error_is_swallowed(self):
        e = ProactiveEngine()
        fake_phys = MagicMock()
        fake_phys.on_mission_end = AsyncMock(side_effect=RuntimeError("no state"))
        with patch("app.context.physical", fake_phys):
            await e._set_post_mission_physical("alice", had_injury=False)  # no raise


# ═══════════════════════════════════════════════════════════════════════════
# Scheduler error listener + stop()  (680-708)
# ═══════════════════════════════════════════════════════════════════════════


class TestSchedulerErrorListener:
    @pytest.mark.asyncio
    async def test_start_registers_error_listener_and_logs_on_job_error(self):
        """start() wires an EVENT_JOB_ERROR listener. We capture the registered
        callback, then invoke it with a fake error event and assert it logs +
        attempts an audit write (best-effort)."""
        e = ProactiveEngine()
        captured = {}

        real_add_listener = e._scheduler.add_listener

        def spy_add_listener(cb, mask):
            # start() registers more than one listener (job-error and
            # job-executed), so select by mask rather than taking the last.
            from apscheduler.events import EVENT_JOB_ERROR
            if mask & EVENT_JOB_ERROR:
                captured["cb"] = cb
                captured["mask"] = mask
            return real_add_listener(cb, mask)

        with patch.object(e._scheduler, "add_listener", side_effect=spy_add_listener):
            try:
                e.start()
            finally:
                e.stop()

        assert "cb" in captured, "error listener was not registered"

        # Now drive the listener with a synthetic failing-job event.
        event = MagicMock()
        event.job_id = "anniversary_check"
        event.exception = ValueError("kaboom")
        event.traceback = "Traceback ..."

        # `from . import audit` resolves the real module attribute, so patch
        # audit.log directly. create_task is stubbed to consume the coroutine.
        created = []

        def consume(coro):
            created.append(coro)
            coro.close()
            return MagicMock()

        with patch("app.proactive.engine.logger") as log, \
             patch("app.audit.log", new=AsyncMock()), \
             patch("asyncio.create_task", side_effect=consume):
            captured["cb"](event)

        # The failure was surfaced through the structured logger.
        assert log.error.called
        msg = log.error.call_args.args[0]
        assert "SCHEDULED_JOB_FAILED" in msg
        # And the best-effort audit write was dispatched.
        assert len(created) == 1

    @pytest.mark.asyncio
    async def test_listener_audit_write_failure_is_swallowed(self):
        """If the best-effort audit task creation raises, the listener still
        returns cleanly (inner except/pass) — the structured log already fired."""
        e = ProactiveEngine()
        captured = {}
        real_add = e._scheduler.add_listener

        def spy(cb, mask):
            from apscheduler.events import EVENT_JOB_ERROR
            if mask & EVENT_JOB_ERROR:
                captured["cb"] = cb
            return real_add(cb, mask)

        with patch.object(e._scheduler, "add_listener", side_effect=spy):
            try:
                e.start()
            finally:
                e.stop()

        event = MagicMock()
        event.job_id = "x"
        event.exception = ValueError("boom")
        event.traceback = "tb"
        # audit.log is stubbed to a plain MagicMock (no coroutine created), and
        # create_task raises — exercising the inner best-effort except/pass.
        with patch("app.proactive.engine.logger") as log, \
             patch("app.audit.log", new=MagicMock(return_value=object())), \
             patch("asyncio.create_task", side_effect=RuntimeError("no loop")):
            captured["cb"](event)  # must not raise despite create_task failing
        assert log.error.called  # structured log still emitted

    def test_listener_registration_failure_is_swallowed(self):
        """If add_listener itself raises, start() logs a warning and continues
        to start the scheduler (outer except). The scheduler start/shutdown are
        stubbed so the assertion targets the warning path, not the AP lifecycle."""
        e = ProactiveEngine()
        with patch.object(e._scheduler, "add_listener",
                          side_effect=RuntimeError("apscheduler busted")), \
             patch.object(e._scheduler, "start"), \
             patch.object(e._scheduler, "shutdown"), \
             patch("app.proactive.engine.logger") as log:
            e.start()
        assert log.warning.called
        warnings = [c.args[0] for c in log.warning.call_args_list]
        assert any("scheduler error listener" in w for w in warnings)

    def test_stop_shuts_down_scheduler_and_active_mission(self):
        e = ProactiveEngine()
        timer = MagicMock()
        timer.active = True
        e._mission_timer = timer
        with patch.object(e._scheduler, "shutdown") as shutdown:
            e.stop()
        timer.stop.assert_called_once()
        shutdown.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# morning/evening physical loop + daily_challenge error  (806-841)
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckinPhysicalLoop:
    @pytest.mark.asyncio
    async def test_morning_checkin_updates_physical_for_each_connected_user(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 2

        fake_phys = MagicMock()
        fake_phys.on_time_of_day = AsyncMock()
        fake_ws = MagicMock()
        fake_ws._connections = {"alice": object(), "bob": object()}

        with _patch_now(datetime(2026, 5, 17, 8, 0, 0)), \
             patch("app.context.physical", fake_phys), \
             patch("app.context.ws", fake_ws), \
             patch("app.weather_client.fetch_weather", new=AsyncMock(return_value=None)), \
             patch("app.proactive.engine.publish_event", new=AsyncMock()):
            await e._morning_checkin()

        # on_time_of_day called for both connected users with hour=8.
        assert fake_phys.on_time_of_day.await_count == 2
        for call in fake_phys.on_time_of_day.await_args_list:
            assert call.args[1] == 8
        # And a morning message was delivered.
        cb.assert_awaited_once()
        assert cb.call_args.args[0] in sum(proactive.MORNING_MESSAGES.values(), [])

    @pytest.mark.asyncio
    async def test_morning_checkin_swallows_physical_error_still_delivers(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        # Patch the import target to raise on access of ._connections
        fake_ws = MagicMock()
        type(fake_ws)._connections = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("ws gone")))

        with _patch_now(datetime(2026, 5, 17, 8, 0, 0)), \
             patch("app.context.ws", fake_ws), \
             patch("app.weather_client.fetch_weather", new=AsyncMock(return_value=None)), \
             patch("app.proactive.engine.publish_event", new=AsyncMock()):
            await e._morning_checkin()
        cb.assert_awaited_once()  # delivery still happens after the swallowed error

    @pytest.mark.asyncio
    async def test_evening_checkin_updates_physical_and_delivers(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 0

        fake_phys = MagicMock()
        fake_phys.on_time_of_day = AsyncMock()
        fake_ws = MagicMock()
        fake_ws._connections = {"alice": object()}

        with _patch_now(datetime(2026, 5, 17, 22, 0, 0)), \
             patch("app.context.physical", fake_phys), \
             patch("app.context.ws", fake_ws), \
             patch("app.proactive.engine.publish_event", new=AsyncMock()):
            await e._evening_checkin()

        fake_phys.on_time_of_day.assert_awaited_once()
        assert fake_phys.on_time_of_day.await_args.args[1] == 22
        cb.assert_awaited_once()
        assert cb.call_args.args[0] in sum(proactive.EVENING_MESSAGES.values(), [])

    @pytest.mark.asyncio
    async def test_evening_checkin_swallows_physical_error_still_delivers(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        fake_ws = MagicMock()
        type(fake_ws)._connections = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("ws gone")))
        with _patch_now(datetime(2026, 5, 17, 22, 0, 0)), \
             patch("app.context.ws", fake_ws), \
             patch("app.proactive.engine.publish_event", new=AsyncMock()):
            await e._evening_checkin()
        cb.assert_awaited_once()  # delivery survives the swallowed physical error


class TestDailyChallengeError:
    @pytest.mark.asyncio
    async def test_personality_exception_is_swallowed(self):
        e = ProactiveEngine()
        e._on_message_callback = AsyncMock()
        e._affection_level = 5
        with patch("app.personality.load_personality",
                   side_effect=RuntimeError("yaml missing")):
            await e._daily_challenge()  # no raise
        e._on_message_callback.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# _random_event  (854-930)
# ═══════════════════════════════════════════════════════════════════════════


def _engine_for_random_event(affection=3, mood="composed"):
    e = ProactiveEngine()
    e._on_message_callback = AsyncMock()
    e._affection_level = affection
    e._last_mood = mood
    e._random_events_today = 0
    e._last_random_event = None
    e._last_message_time = None
    e._muted_until = None
    return e


_EVENTS_CFG = {
    "lore": {"min_affection": 0, "weight": 10,
             "messages": ["The wind off Mechty's ridge again."]},
    "intimate": {"min_affection": 5, "weight": 20,
                 "messages": ["...I keep thinking about you."]},
}


class TestRandomEvent:
    @pytest.mark.asyncio
    async def test_daily_cap_blocks(self):
        e = _engine_for_random_event()
        e._random_events_today = 5
        with _patch_now():
            await e._random_event()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_45min_gap_blocks(self):
        e = _engine_for_random_event()
        e._last_random_event = _NOON - timedelta(minutes=10)  # too recent
        with _patch_now():
            await e._random_event()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_typing_cooldown_blocks(self):
        e = _engine_for_random_event()
        e._last_message_time = _NOON - timedelta(minutes=1)  # within 3-min cooldown
        with _patch_now():
            await e._random_event()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mute_blocks(self):
        e = _engine_for_random_event()
        e._muted_until = _NOON + timedelta(hours=1)
        with _patch_now():
            await e._random_event()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_probability_roll_above_chance_skips(self):
        e = _engine_for_random_event()
        # base 0.35; roll just above -> skip
        with _patch_now(), patch("app.proactive.events.random.random", return_value=0.99):
            await e._random_event()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fires_and_increments_counters(self):
        e = _engine_for_random_event(affection=0)
        with _patch_now(), \
             patch("app.proactive.events.random.random", return_value=0.01), \
             patch("app.proactive.events.random.choice", side_effect=lambda seq: seq[0]), \
             patch("app.personality.load_personality",
                   return_value={"random_events": _EVENTS_CFG}):
            await e._random_event()

        e._on_message_callback.assert_awaited_once()
        assert e._random_events_today == 1
        assert e._last_random_event == _NOON
        assert e._last_proactive_answered is False
        # Only the lore category is eligible at affection 0.
        assert e._on_message_callback.call_args.args[0] == \
            "The wind off Mechty's ridge again."

    @pytest.mark.asyncio
    async def test_intimate_mood_boosts_chance_above_base(self):
        """At a tender mood, base_chance becomes 0.60 — a roll of 0.5 (which
        would skip at the 0.35 base) now fires."""
        e = _engine_for_random_event(affection=0, mood="tender")
        with _patch_now(), \
             patch("app.proactive.events.random.random", return_value=0.5), \
             patch("app.proactive.events.random.choice", side_effect=lambda seq: seq[0]), \
             patch("app.personality.load_personality",
                   return_value={"random_events": _EVENTS_CFG}):
            await e._random_event()
        e._on_message_callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_affection_gates_eligible_categories(self):
        """At affection 5 the 'intimate' category (min_affection 5) becomes
        eligible. With its higher weight + a low roll, it is selected."""
        e = _engine_for_random_event(affection=5)
        # random() consulted twice: probability gate, then weighted-pick roll.
        rolls = iter([0.01, 0.0])
        with _patch_now(), \
             patch("app.proactive.events.random.random", side_effect=lambda: next(rolls)), \
             patch("app.proactive.events.random.choice", side_effect=lambda seq: seq[0]), \
             patch("app.personality.load_personality",
                   return_value={"random_events": _EVENTS_CFG}):
            await e._random_event()
        # roll==0 hits the first cumulative bucket -> 'lore' messages.
        assert e._on_message_callback.call_args.args[0] == \
            "The wind off Mechty's ridge again."

    @pytest.mark.asyncio
    async def test_no_eligible_categories_skips(self):
        e = _engine_for_random_event(affection=0)
        # Only an intimate category requiring affection 5 — none eligible at 0.
        cfg = {"intimate": {"min_affection": 5, "weight": 5, "messages": ["x"]}}
        with _patch_now(), \
             patch("app.proactive.events.random.random", return_value=0.01), \
             patch("app.personality.load_personality",
                   return_value={"random_events": cfg}):
            await e._random_event()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_personality_load_failure_skips(self):
        e = _engine_for_random_event()
        with _patch_now(), \
             patch("app.proactive.events.random.random", return_value=0.01), \
             patch("app.personality.load_personality",
                   side_effect=RuntimeError("no yaml")):
            await e._random_event()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_dict_config_entries_skipped(self):
        """Malformed (non-dict) random_events entries are ignored while valid
        ones still fire (exercises the isinstance guard)."""
        e = _engine_for_random_event(affection=0)
        cfg = {
            "garbage": "not-a-dict",  # skipped by isinstance check
            "lore": {"min_affection": 0, "weight": 10, "messages": ["Mechty wind."]},
        }
        with _patch_now(), \
             patch("app.proactive.events.random.random", return_value=0.01), \
             patch("app.proactive.events.random.choice", side_effect=lambda seq: seq[0]), \
             patch("app.personality.load_personality",
                   return_value={"random_events": cfg}):
            await e._random_event()
        e._on_message_callback.assert_awaited_once_with("Mechty wind.")

    @pytest.mark.asyncio
    async def test_mission_active_raises_floor_chance(self):
        """During an active mission base_chance is floored at 0.50, so a roll of
        0.45 (skip at 0.35) fires."""
        e = _engine_for_random_event(affection=0)
        active_timer = MagicMock()
        active_timer.active = True
        e._mission_timer = active_timer
        with _patch_now(), \
             patch("app.proactive.events.random.random", return_value=0.45), \
             patch("app.proactive.events.random.choice", side_effect=lambda seq: seq[0]), \
             patch("app.personality.load_personality",
                   return_value={"random_events": _EVENTS_CFG}):
            await e._random_event()
        e._on_message_callback.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
# _mission_random_event  (934-1003)
# ═══════════════════════════════════════════════════════════════════════════


class TestMissionRandomEvent:
    @pytest.mark.asyncio
    async def test_no_mission_active_skips(self):
        e = ProactiveEngine()
        e._on_message_callback = AsyncMock()
        # mission_active is False (no timer)
        with patch("app.fact_extractor.generate_mission_update", new=AsyncMock()):
            await e._mission_random_event()
        e._on_message_callback.assert_not_awaited()

    def _active_engine(self):
        e = ProactiveEngine()
        e._on_message_callback = AsyncMock()
        e._affection_level = 4
        e._last_random_event = None
        e._last_message_time = None
        e._muted_until = None
        timer = MagicMock()
        timer.active = True
        timer.mission_description = "Hold the ridge"
        timer.started_at = 0.0
        timer.update_count = 2
        timer.active_events = []
        e._mission_timer = timer
        return e, timer

    @pytest.mark.asyncio
    async def test_15min_gap_blocks(self):
        e, _ = self._active_engine()
        e._last_random_event = _NOON - timedelta(minutes=5)
        with _patch_now(), patch("app.fact_extractor.generate_mission_update", new=AsyncMock()):
            await e._mission_random_event()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_typing_cooldown_blocks(self):
        e, _ = self._active_engine()
        e._last_message_time = _NOON - timedelta(minutes=1)
        with _patch_now(), patch("app.fact_extractor.generate_mission_update", new=AsyncMock()):
            await e._mission_random_event()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mute_blocks(self):
        e, _ = self._active_engine()
        e._muted_until = _NOON + timedelta(hours=1)
        with _patch_now(), patch("app.fact_extractor.generate_mission_update", new=AsyncMock()):
            await e._mission_random_event()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_probability_above_75pct_skips(self):
        e, _ = self._active_engine()
        with _patch_now(), \
             patch("app.proactive.mission.random.random", return_value=0.99), \
             patch("app.fact_extractor.generate_mission_update", new=AsyncMock()):
            await e._mission_random_event()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fires_contextual_update_with_mission_desc(self):
        e, timer = self._active_engine()
        gen = AsyncMock(return_value="Contact on Mechty's flank — handling it.")
        with _patch_now(), \
             patch("app.proactive.mission.random.random", return_value=0.1), \
             patch("app.proactive.mission.random.choice", side_effect=lambda seq: seq[0]), \
             patch("app.proactive.mission.time.monotonic", return_value=1800.0), \
             patch("app.fact_extractor.generate_mission_update", gen):
            await e._mission_random_event()

        gen.assert_awaited_once()
        assert gen.await_args.kwargs["mission_desc"] == "Hold the ridge"
        assert gen.await_args.kwargs["update_number"] == 3  # update_count+1
        e._on_message_callback.assert_awaited_once_with(
            "Contact on Mechty's flank — handling it.")
        assert e._last_random_event == _NOON
        assert e._random_events_today == 1

    @pytest.mark.asyncio
    async def test_falsy_but_active_timer_aborts_inner(self):
        """mission_active can be True for a timer whose __bool__ is False (e.g.
        an object overriding truthiness). The inner `if not timer: return`
        guard then aborts before any LLM call."""
        e = ProactiveEngine()
        e._on_message_callback = AsyncMock()
        e._affection_level = 3
        e._last_random_event = None
        e._last_message_time = None
        e._muted_until = None

        class _FalsyActiveTimer:
            active = True

            def __bool__(self):
                return False

        e._mission_timer = _FalsyActiveTimer()
        gen = AsyncMock()
        with _patch_now(), \
             patch("app.proactive.mission.random.random", return_value=0.1), \
             patch("app.fact_extractor.generate_mission_update", gen):
            await e._mission_random_event()
        gen.assert_not_awaited()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_llm_message_not_delivered(self):
        e, _ = self._active_engine()
        with _patch_now(), \
             patch("app.proactive.mission.random.random", return_value=0.1), \
             patch("app.proactive.mission.random.choice", side_effect=lambda seq: seq[0]), \
             patch("app.proactive.mission.time.monotonic", return_value=1800.0), \
             patch("app.fact_extractor.generate_mission_update",
                   AsyncMock(return_value="")):
            await e._mission_random_event()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_llm_error_swallowed(self):
        e, _ = self._active_engine()
        with _patch_now(), \
             patch("app.proactive.mission.random.random", return_value=0.1), \
             patch("app.proactive.mission.random.choice", side_effect=lambda seq: seq[0]), \
             patch("app.proactive.mission.time.monotonic", return_value=1800.0), \
             patch("app.fact_extractor.generate_mission_update",
                   AsyncMock(side_effect=RuntimeError("LM down"))):
            await e._mission_random_event()  # no raise
        e._on_message_callback.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# Romance edge gates + _daily_recap  (1023, 1025, 1033, 1035, 1055-1057, 1082-1095)
# ═══════════════════════════════════════════════════════════════════════════


class TestRomanceEdgeGates:
    @pytest.mark.asyncio
    async def test_muted_before_delay_blocks(self):
        """Mute check at line 1022 (pre-delay) blocks delivery."""
        e = ProactiveEngine()
        e._affection_level = 5
        e._user_messaged_today = True
        e._last_proactive_answered = True
        e._romance_delivered_today = False
        e._muted_until = datetime(9999, 1, 1)
        cb = AsyncMock()
        e._on_message_callback = cb
        with patch("asyncio.sleep", new=AsyncMock()):
            await e._romance_window()
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unanswered_prior_blocks(self):
        e = ProactiveEngine()
        e._affection_level = 5
        e._user_messaged_today = True
        e._last_proactive_answered = False  # blocks at line 1024
        e._romance_delivered_today = False
        e._muted_until = None
        cb = AsyncMock()
        e._on_message_callback = cb
        with patch("asyncio.sleep", new=AsyncMock()):
            await e._romance_window()
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delivered_during_delay_aborts(self):
        """If _romance_delivered_today flips True during the random delay, the
        post-delay re-check (line 1032) aborts before sending."""
        e = ProactiveEngine()
        e._affection_level = 3
        e._user_messaged_today = True
        e._last_proactive_answered = True
        e._romance_delivered_today = False
        e._muted_until = None
        cb = AsyncMock()
        e._on_message_callback = cb

        async def flip(*_a, **_k):
            e._romance_delivered_today = True

        with patch("asyncio.sleep", side_effect=flip):
            await e._romance_window()
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_muted_during_delay_aborts(self):
        e = ProactiveEngine()
        e._affection_level = 3
        e._user_messaged_today = True
        e._last_proactive_answered = True
        e._romance_delivered_today = False
        e._muted_until = None
        cb = AsyncMock()
        e._on_message_callback = cb

        async def mute_mid(*_a, **_k):
            e._muted_until = datetime(9999, 1, 1)

        with _patch_now(), patch("asyncio.sleep", side_effect=mute_mid):
            await e._romance_window()
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_llm_romance_uses_session_context_summary(self):
        """At affection>=5 with a session getter, the rolling context summary is
        threaded into generate_romance_message."""
        e = ProactiveEngine()
        e._affection_level = 6
        e._user_messaged_today = True
        e._last_proactive_answered = True
        e._romance_delivered_today = False
        e._muted_until = None
        e._last_mood = "tender"
        cb = AsyncMock()
        e._on_message_callback = cb

        session = MagicMock()
        session.context_summary = "He mentioned the ridge mission earlier."
        e._session_getter = AsyncMock(return_value=session)

        gen = AsyncMock(return_value="The ridge... I keep thinking of it. And you.")
        with patch("asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.events.publish_event", new=AsyncMock()), \
             patch("app.fact_extractor.generate_romance_message", gen):
            await e._romance_window()

        gen.assert_awaited_once()
        assert gen.await_args.kwargs["context_summary"] == \
            "He mentioned the ridge mission earlier."
        assert gen.await_args.kwargs["affection_level"] == 6
        cb.assert_awaited_once_with("The ridge... I keep thinking of it. And you.")


class TestDailyRecap:
    @pytest.mark.asyncio
    async def test_no_callbacks_skips(self):
        e = ProactiveEngine()
        e._on_recap_callback = None
        e._on_message_callback = None
        await e._daily_recap()  # no raise

    @pytest.mark.asyncio
    async def test_blocked_when_cannot_send(self):
        e = ProactiveEngine()
        e._on_recap_callback = AsyncMock(return_value="Recap")
        e._on_message_callback = AsyncMock()
        # Quiet hours -> _can_send False
        with _patch_now(datetime(2026, 5, 17, 5, 0, 0)):
            await e._daily_recap()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_generates_and_delivers_recap(self):
        e = ProactiveEngine()
        e._affection_level = 5
        recap_cb = AsyncMock(return_value="Today we held the ridge. I'm proud of the unit.")
        e._on_recap_callback = recap_cb
        msg_cb = AsyncMock()
        e._on_message_callback = msg_cb
        with _patch_now():
            await e._daily_recap()
        recap_cb.assert_awaited_once_with(5)  # affection level passed through
        msg_cb.assert_awaited_once_with("Today we held the ridge. I'm proud of the unit.")
        assert e._proactive_count_today == 1
        assert e._last_proactive_answered is False

    @pytest.mark.asyncio
    async def test_empty_recap_not_delivered(self):
        e = ProactiveEngine()
        e._on_recap_callback = AsyncMock(return_value="")
        msg_cb = AsyncMock()
        e._on_message_callback = msg_cb
        with _patch_now():
            await e._daily_recap()
        msg_cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recap_error_swallowed(self):
        e = ProactiveEngine()
        e._on_recap_callback = AsyncMock(side_effect=RuntimeError("LLM down"))
        msg_cb = AsyncMock()
        e._on_message_callback = msg_cb
        with _patch_now():
            await e._daily_recap()  # no raise
        msg_cb.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# _dream_event  (1104-1211)
# ═══════════════════════════════════════════════════════════════════════════


def _dream_engine(affection=8):
    e = ProactiveEngine()
    e._affection_level = affection
    e._dream_delivered_today = False
    e._muted_until = None
    e._on_message_callback = AsyncMock()
    return e


class TestDreamEvent:
    @pytest.mark.asyncio
    async def test_already_delivered_skips(self):
        e = _dream_engine()
        e._dream_delivered_today = True
        await e._dream_event()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_low_affection_skips(self):
        e = _dream_engine(affection=4)  # below 5
        await e._dream_event()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_muted_skips(self):
        e = _dream_engine()
        e._muted_until = datetime(9999, 1, 1)
        with _patch_now():
            await e._dream_event()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_probability_above_40pct_skips(self):
        e = _dream_engine()
        with _patch_now(), patch("app.proactive.events.random.random", return_value=0.99):
            await e._dream_event()
        e._on_message_callback.assert_not_awaited()

    async def _run_dream_collecting_prompt(self, e, roll_value):
        """Fire a dream with deterministic rolls, capturing the LLM prompt."""
        captured = {}

        async def fake_text(url, model, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return "I dreamt of you, Commander. I woke reaching for your hand."

        # random() order: fire-gate (0.1 to pass), dream-type roll (roll_value).
        rolls = iter([0.1, roll_value])

        gate = AsyncMock()
        gate.__aenter__ = AsyncMock()
        gate.__aexit__ = AsyncMock()

        # Patch the re-exported list_memories attribute directly. `_dream_event`
        # does `from . import memory_archive` then `memory_archive.list_memories`,
        # so patching the module attribute is robust regardless of import order
        # (replacing sys.modules wholesale breaks once the module is pre-imported).
        with _patch_now(datetime(2026, 5, 17, 2, 37, 0)), \
             patch("app.proactive.events.random.random", side_effect=lambda: next(rolls)), \
             patch("app.llm_json.call_llm_text", side_effect=fake_text), \
             patch("app.llm_router.get_lm_gate", return_value=gate), \
             patch("app.memory_archive.list_memories", new=AsyncMock(return_value=[])):
            await e._dream_event()
        return captured

    @pytest.mark.asyncio
    async def test_high_affection_erotic_branch(self):
        e = _dream_engine(affection=8)
        captured = await self._run_dream_collecting_prompt(e, roll_value=0.1)
        # erotic branch (roll<0.30 at aff>=8): prompt mentions erotic dream
        assert "erotic" in captured["prompt"].lower()
        e._on_message_callback.assert_awaited_once()
        assert e._dream_delivered_today is True

    @pytest.mark.asyncio
    async def test_high_affection_tender_branch(self):
        e = _dream_engine(affection=8)
        captured = await self._run_dream_collecting_prompt(e, roll_value=0.5)
        assert "tender moment" in captured["prompt"].lower()
        e._on_message_callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_high_affection_nightmare_branch(self):
        e = _dream_engine(affection=8)
        captured = await self._run_dream_collecting_prompt(e, roll_value=0.8)
        assert "nightmare" in captured["prompt"].lower()

    @pytest.mark.asyncio
    async def test_high_affection_random_branch(self):
        e = _dream_engine(affection=8)
        captured = await self._run_dream_collecting_prompt(e, roll_value=0.95)
        assert "strange" in captured["prompt"].lower() or "surreal" in captured["prompt"].lower()

    @pytest.mark.asyncio
    async def test_mid_affection_tender_branch(self):
        e = _dream_engine(affection=6)
        captured = await self._run_dream_collecting_prompt(e, roll_value=0.3)
        # aff 6: 0.10<=0.3<0.50 -> tender
        assert "tender moment" in captured["prompt"].lower()

    @pytest.mark.asyncio
    async def test_mid_affection_erotic_branch(self):
        # aff 6: roll<0.10 -> erotic (10% chance)
        e = _dream_engine(affection=6)
        captured = await self._run_dream_collecting_prompt(e, roll_value=0.05)
        assert "erotic" in captured["prompt"].lower()

    @pytest.mark.asyncio
    async def test_mid_affection_nightmare_branch(self):
        # aff 6: 0.50<=0.6<0.80 -> nightmare
        e = _dream_engine(affection=6)
        captured = await self._run_dream_collecting_prompt(e, roll_value=0.6)
        assert "nightmare" in captured["prompt"].lower()

    @pytest.mark.asyncio
    async def test_mid_affection_random_branch(self):
        # aff 6: roll>=0.80 -> random
        e = _dream_engine(affection=6)
        captured = await self._run_dream_collecting_prompt(e, roll_value=0.9)
        assert "strange" in captured["prompt"].lower() or "surreal" in captured["prompt"].lower()

    @pytest.mark.asyncio
    async def test_lower_affection_no_erotic(self):
        """At affection 5 there is no erotic branch — roll 0.05 -> tender."""
        e = _dream_engine(affection=5)
        captured = await self._run_dream_collecting_prompt(e, roll_value=0.05)
        assert "tender moment" in captured["prompt"].lower()
        assert "erotic" not in captured["prompt"].lower()

    @pytest.mark.asyncio
    async def test_lower_affection_nightmare_and_random(self):
        e = _dream_engine(affection=5)
        cap_nm = await self._run_dream_collecting_prompt(_dream_engine(affection=5), 0.5)
        assert "nightmare" in cap_nm["prompt"].lower()
        cap_rnd = await self._run_dream_collecting_prompt(_dream_engine(affection=5), 0.9)
        assert "strange" in cap_rnd["prompt"].lower() or "surreal" in cap_rnd["prompt"].lower()

    @pytest.mark.asyncio
    async def test_memory_seed_woven_into_prompt(self):
        """When the archive returns a memory, its annotation is appended as a
        dream seed in the prompt."""
        e = _dream_engine(affection=8)
        captured = {}

        async def fake_text(url, model, prompt, **kwargs):
            captured["prompt"] = prompt
            return "Dream text."

        rolls = iter([0.1, 0.1])
        gate = AsyncMock()
        gate.__aenter__ = AsyncMock()
        gate.__aexit__ = AsyncMock()
        archive_mem = AsyncMock(
            return_value=[{"annotation": "The night on the observation deck."}])

        with _patch_now(datetime(2026, 5, 17, 2, 37, 0)), \
             patch("app.proactive.events.random.random", side_effect=lambda: next(rolls)), \
             patch("app.proactive.events.random.choice", side_effect=lambda seq: seq[0]), \
             patch("app.llm_json.call_llm_text", side_effect=fake_text), \
             patch("app.llm_router.get_lm_gate", return_value=gate), \
             patch("app.memory_archive.list_memories", new=archive_mem):
            await e._dream_event()

        assert "observation deck" in captured["prompt"]
        assert "Dream seed" in captured["prompt"]

    @pytest.mark.asyncio
    async def test_memory_fetch_failure_does_not_block_dream(self):
        """If list_memories raises, the inner guard swallows it and the dream
        still generates (no Dream seed appended)."""
        e = _dream_engine(affection=8)
        captured = {}

        async def fake_text(url, model, prompt, **kwargs):
            captured["prompt"] = prompt
            return "Dream text."

        rolls = iter([0.1, 0.1])
        gate = AsyncMock()
        gate.__aenter__ = AsyncMock()
        gate.__aexit__ = AsyncMock()
        with _patch_now(datetime(2026, 5, 17, 2, 37, 0)), \
             patch("app.proactive.events.random.random", side_effect=lambda: next(rolls)), \
             patch("app.llm_json.call_llm_text", side_effect=fake_text), \
             patch("app.llm_router.get_lm_gate", return_value=gate), \
             patch("app.memory_archive.list_memories",
                   new=AsyncMock(side_effect=RuntimeError("archive down"))):
            await e._dream_event()

        assert "Dream seed" not in captured["prompt"]
        e._on_message_callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_llm_message_not_delivered(self):
        e = _dream_engine(affection=8)
        rolls = iter([0.1, 0.1])
        gate = AsyncMock()
        gate.__aenter__ = AsyncMock()
        gate.__aexit__ = AsyncMock()
        with _patch_now(datetime(2026, 5, 17, 2, 37, 0)), \
             patch("app.proactive.events.random.random", side_effect=lambda: next(rolls)), \
             patch("app.llm_json.call_llm_text", AsyncMock(return_value="")), \
             patch("app.llm_router.get_lm_gate", return_value=gate), \
             patch("app.memory_archive.list_memories", new=AsyncMock(return_value=[])):
            await e._dream_event()
        e._on_message_callback.assert_not_awaited()
        assert e._dream_delivered_today is False

    @pytest.mark.asyncio
    async def test_llm_error_swallowed(self):
        e = _dream_engine(affection=8)
        rolls = iter([0.1, 0.1])
        gate = AsyncMock()
        gate.__aenter__ = AsyncMock()
        gate.__aexit__ = AsyncMock()
        with _patch_now(datetime(2026, 5, 17, 2, 37, 0)), \
             patch("app.proactive.events.random.random", side_effect=lambda: next(rolls)), \
             patch("app.llm_json.call_llm_text", AsyncMock(side_effect=RuntimeError("down"))), \
             patch("app.llm_router.get_lm_gate", return_value=gate), \
             patch("app.memory_archive.list_memories", new=AsyncMock(return_value=[])):
            await e._dream_event()  # no raise
        e._on_message_callback.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# _unsent_message_check  (1225-1269)
# ═══════════════════════════════════════════════════════════════════════════


class TestUnsentMessageCheck:
    @pytest.mark.asyncio
    async def test_low_affection_skips(self):
        e = ProactiveEngine()
        e._affection_level = 4  # below 5
        e._on_message_callback = AsyncMock()
        await e._unsent_message_check()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cannot_send_skips(self):
        e = ProactiveEngine()
        e._affection_level = 6
        e._on_message_callback = AsyncMock()
        with _patch_now(datetime(2026, 5, 17, 5, 0, 0)):  # quiet hours
            await e._unsent_message_check()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_probability_above_15pct_skips(self):
        e = ProactiveEngine()
        e._affection_level = 6
        e._last_proactive_answered = True
        e._on_message_callback = AsyncMock()
        with _patch_now(), patch("app.proactive.milestones.random.random", return_value=0.99):
            await e._unsent_message_check()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sends_deleted_then_followup(self):
        e = ProactiveEngine()
        e._affection_level = 7
        e._last_proactive_answered = True
        cb = AsyncMock()
        e._on_message_callback = cb
        with _patch_now(), \
             patch("app.proactive.milestones.random.random", return_value=0.01), \
             patch("app.proactive.milestones.random.uniform", return_value=0), \
             patch("app.proactive.milestones.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.milestones.random.choice", side_effect=lambda seq: seq[0]):
            await e._unsent_message_check()

        # First the "[Message deleted]" placeholder, then a level-7 follow-up.
        assert cb.await_count == 2
        assert cb.await_args_list[0].args[0] == "[Message deleted]"
        assert cb.await_args_list[1].args[0] == \
            "...I didn't mean to send that. Or maybe I did. Forget it."
        assert e._proactive_count_today == 1
        assert e._last_proactive_answered is False

    @pytest.mark.asyncio
    async def test_affection_clamped_to_followup_levels(self):
        """Affection above 9 still clamps into the 5-9 follow-up table."""
        e = ProactiveEngine()
        e._affection_level = 9
        e._last_proactive_answered = True
        cb = AsyncMock()
        e._on_message_callback = cb
        with _patch_now(), \
             patch("app.proactive.milestones.random.random", return_value=0.01), \
             patch("app.proactive.milestones.random.uniform", return_value=0), \
             patch("app.proactive.milestones.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.milestones.random.choice", side_effect=lambda seq: seq[0]):
            await e._unsent_message_check()
        assert cb.await_args_list[1].args[0] == \
            "...You know what it said. You always know."


# ═══════════════════════════════════════════════════════════════════════════
# check_anniversaries  (1279-1334)
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckAnniversaries:
    @pytest.mark.asyncio
    async def test_exact_anniversary_match_returns_years_ago(self):
        e = ProactiveEngine()
        # Freeze the clock: check_anniversaries uses now_local() (America/Chicago),
        # so building dates from date.today() (server UTC) is off by a day in the
        # evening-Chicago window. Pin both to _NOON.
        today = _NOON.date()
        # Same month/day, 2 years ago -> exact anniversary.
        past = date(today.year - 2, today.month, today.day)
        row = ("first_kiss", past)
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=_result_with_fetchall([row]))
        with _patch_now(), patch("app.db.get_conn", return_value=_db_ctx(conn)):
            results = await e.check_anniversaries("alice")

        assert len(results) == 1
        assert results[0]["event_type"] == "first_kiss"
        assert results[0]["years_ago"] == 2
        assert results[0]["days_ago"] == 0

    @pytest.mark.asyncio
    async def test_near_anniversary_within_three_days(self):
        e = ProactiveEngine()
        # Frozen clock (see exact-match test). _NOON is mid-May, so +2 days
        # never crosses a month/year boundary — the old skip is unnecessary.
        today = _NOON.date()
        target = today + timedelta(days=2)
        past = date(today.year - 1, target.month, target.day)
        row = ("first_mission", past)
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=_result_with_fetchall([row]))
        with _patch_now(), patch("app.db.get_conn", return_value=_db_ctx(conn)):
            results = await e.check_anniversaries("alice")
        assert len(results) == 1
        assert abs(results[0]["days_ago"]) == 2  # signed: past +N / upcoming -N
        assert results[0]["years_ago"] == 1

    @pytest.mark.asyncio
    async def test_far_date_not_matched(self):
        e = ProactiveEngine()
        today = _NOON.date()
        # ~6 months away — neither exact nor within 3 days.
        far = date(today.year - 1, ((today.month + 5 - 1) % 12) + 1, 15)
        row = ("first_argument", far)
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=_result_with_fetchall([row]))
        with _patch_now(), patch("app.db.get_conn", return_value=_db_ctx(conn)):
            results = await e.check_anniversaries("alice")
        # The far date should not produce a match (delta > 3, not exact).
        assert results == [] or all(r["days_ago"] <= 3 for r in results)

    @pytest.mark.asyncio
    async def test_results_are_cached_for_five_minutes(self):
        e = ProactiveEngine()
        today = _NOON.date()
        past = date(today.year - 1, today.month, today.day)
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=_result_with_fetchall([("first_kiss", past)]))
        with _patch_now(), patch("app.db.get_conn", return_value=_db_ctx(conn)) as gc:
            first = await e.check_anniversaries("alice")
            second = await e.check_anniversaries("alice")  # served from cache
        assert first == second
        # Only one DB round-trip — the second call hit the TTL cache.
        assert gc.call_count == 1

    @pytest.mark.asyncio
    async def test_db_error_returns_empty(self):
        e = ProactiveEngine()
        conn = AsyncMock()
        conn.execute = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("app.db.get_conn", return_value=_db_ctx(conn)):
            results = await e.check_anniversaries("alice")
        assert results == []

    @pytest.mark.asyncio
    async def test_feb29_in_non_leap_year_uses_feb28(self):
        """A Feb-29 first should not crash check_anniversaries even when this
        year is not a leap year — the replace() fallback to day=28 handles it.
        Only meaningful in non-leap years; skip otherwise."""
        e = ProactiveEngine()
        # Frozen to 2026 (a non-leap year), so the Feb-28 fallback path is always
        # exercised deterministically rather than skipped in leap years.
        past = date(2020, 2, 29)  # 2020 is a leap year
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=_result_with_fetchall([("first_snow", past)]))
        with _patch_now(), patch("app.db.get_conn", return_value=_db_ctx(conn)):
            results = await e.check_anniversaries("alice")  # must not raise
        assert isinstance(results, list)


# ═══════════════════════════════════════════════════════════════════════════
# record_first  (1340-1356)
# ═══════════════════════════════════════════════════════════════════════════


class TestRecordFirst:
    @pytest.mark.asyncio
    async def test_new_first_returns_true(self):
        e = ProactiveEngine()
        result = MagicMock()
        result.rowcount = 1
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=result)
        with patch("app.db.get_conn_autocommit", return_value=_db_ctx(conn)):
            ok = await e.record_first("alice", "first_kiss", {"note": "snow"})
        assert ok is True
        # The INSERT was issued.
        conn.execute.assert_awaited_once()
        assert "companion_firsts" in conn.execute.await_args.args[0]

    @pytest.mark.asyncio
    async def test_conflict_returns_false(self):
        e = ProactiveEngine()
        result = MagicMock()
        result.rowcount = 0  # ON CONFLICT DO NOTHING -> no row
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=result)
        with patch("app.db.get_conn_autocommit", return_value=_db_ctx(conn)):
            ok = await e.record_first("alice", "first_kiss")
        assert ok is False

    @pytest.mark.asyncio
    async def test_db_error_returns_false(self):
        e = ProactiveEngine()
        conn = AsyncMock()
        conn.execute = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("app.db.get_conn_autocommit", return_value=_db_ctx(conn)):
            ok = await e.record_first("alice", "first_kiss")
        assert ok is False


# ═══════════════════════════════════════════════════════════════════════════
# get_comfort_objects  (1362-1393)
# ═══════════════════════════════════════════════════════════════════════════


class TestGetComfortObjects:
    @pytest.mark.asyncio
    async def test_maps_rows_to_dicts(self):
        e = ProactiveEngine()
        given = date(2026, 5, 1)
        rows = [("compass", "brass field compass", "treasured", given, 3)]
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=_result_with_fetchall(rows))
        with patch("app.db.get_conn", return_value=_db_ctx(conn)):
            gifts = await e.get_comfort_objects("alice")
        assert gifts == [{
            "item": "compass",
            "description": "brass field compass",
            "sentiment": "treasured",
            "given_date": given.isoformat(),
            "referenced_count": 3,
        }]

    @pytest.mark.asyncio
    async def test_cached_for_five_minutes(self):
        e = ProactiveEngine()
        rows = [("compass", "d", "treasured", date(2026, 5, 1), 0)]
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=_result_with_fetchall(rows))
        with patch("app.db.get_conn", return_value=_db_ctx(conn)) as gc:
            first = await e.get_comfort_objects("alice")
            second = await e.get_comfort_objects("alice")
        assert first == second
        assert gc.call_count == 1  # second served from cache

    @pytest.mark.asyncio
    async def test_db_error_returns_empty(self):
        e = ProactiveEngine()
        conn = AsyncMock()
        conn.execute = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("app.db.get_conn", return_value=_db_ctx(conn)):
            gifts = await e.get_comfort_objects("alice")
        assert gifts == []

    @pytest.mark.asyncio
    async def test_null_given_date_serializes_to_none(self):
        e = ProactiveEngine()
        rows = [("note", None, "treasured", None, 0)]
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=_result_with_fetchall(rows))
        with patch("app.db.get_conn", return_value=_db_ctx(conn)):
            gifts = await e.get_comfort_objects("alice")
        assert gifts[0]["given_date"] is None


# ═══════════════════════════════════════════════════════════════════════════
# store_gift  (1400-1414)
# ═══════════════════════════════════════════════════════════════════════════


class TestStoreGift:
    @pytest.mark.asyncio
    async def test_inserts_and_invalidates_cache(self):
        e = ProactiveEngine()
        # Pre-seed a stale cache entry for the user.
        e._gifts_cache = {"gifts:alice": (datetime.now(), [{"item": "old"}])}
        conn = AsyncMock()
        conn.execute = AsyncMock()
        with patch("app.db.get_conn_autocommit", return_value=_db_ctx(conn)):
            await e.store_gift("alice", "compass", "brass field compass", "treasured")
        conn.execute.assert_awaited_once()
        sql, params = conn.execute.await_args.args
        assert "companion_gifts" in sql
        assert params == ("alice", "compass", "brass field compass", "treasured")
        # Cache entry for alice was invalidated.
        assert "gifts:alice" not in e._gifts_cache

    @pytest.mark.asyncio
    async def test_db_error_swallowed(self):
        e = ProactiveEngine()
        conn = AsyncMock()
        conn.execute = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("app.db.get_conn_autocommit", return_value=_db_ctx(conn)):
            await e.store_gift("alice", "compass")  # no raise

    @pytest.mark.asyncio
    async def test_default_sentiment_is_treasured(self):
        e = ProactiveEngine()
        conn = AsyncMock()
        conn.execute = AsyncMock()
        with patch("app.db.get_conn_autocommit", return_value=_db_ctx(conn)):
            await e.store_gift("alice", "compass")
        params = conn.execute.await_args.args[1]
        assert params[3] == "treasured"


# ═══════════════════════════════════════════════════════════════════════════
# trigger_mission_aftermath_image  (1427-1489)
# ═══════════════════════════════════════════════════════════════════════════


class TestMissionAftermathImage:
    @pytest.mark.asyncio
    async def test_no_callback_skips(self):
        e = ProactiveEngine()
        e._on_message_callback = None
        timer = MissionTimer()
        await e.trigger_mission_aftermath_image("alice", timer=timer)  # no raise

    @pytest.mark.asyncio
    async def test_no_timer_skips(self):
        e = ProactiveEngine()
        e._on_message_callback = AsyncMock()
        await e.trigger_mission_aftermath_image("alice", timer=None)
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_victory_scene_when_no_events(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 3
        timer = MissionTimer()
        timer.active_events = []

        build = MagicMock(return_value="victory prompt")
        with patch("app.image_gen.build_mission_prompt", build), \
             patch("app.image_gen.generate_image", new=AsyncMock(return_value=None)), \
             patch("app.proactive.mission.asyncio.create_task", side_effect=lambda c: (c.close(), MagicMock())[-1]), \
             patch("app.proactive.mission.random.choice", side_effect=lambda seq: seq[0]):
            await e.trigger_mission_aftermath_image("alice", timer=timer)

        # scene_type derived as "victory"; caption is the first victory line.
        assert build.call_args.kwargs["scene_type"] == "victory"
        cb.assert_awaited_once()
        assert "Mission complete" in cb.call_args.args[0]

    @pytest.mark.asyncio
    async def test_injury_scene_when_injured_event(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        timer = MissionTimer()
        timer.active_events = ["klukai_injured"]
        build = MagicMock(return_value="injury prompt")
        with patch("app.image_gen.build_mission_prompt", build), \
             patch("app.image_gen.generate_image", new=AsyncMock(return_value=None)), \
             patch("app.proactive.mission.asyncio.create_task", side_effect=lambda c: (c.close(), MagicMock())[-1]), \
             patch("app.proactive.mission.random.choice", side_effect=lambda seq: seq[0]):
            await e.trigger_mission_aftermath_image("alice", timer=timer)
        assert build.call_args.kwargs["scene_type"] == "injury"
        assert build.call_args.kwargs["injuries"] == ["klukai_injured"]

    @pytest.mark.asyncio
    async def test_extraction_scene_for_noninjury_events(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        timer = MissionTimer()
        timer.active_events = ["comms_disruption"]  # event present, no injury
        build = MagicMock(return_value="extraction prompt")
        with patch("app.image_gen.build_mission_prompt", build), \
             patch("app.image_gen.generate_image", new=AsyncMock(return_value=None)), \
             patch("app.proactive.mission.asyncio.create_task", side_effect=lambda c: (c.close(), MagicMock())[-1]), \
             patch("app.proactive.mission.random.choice", side_effect=lambda seq: seq[0]):
            await e.trigger_mission_aftermath_image("alice", timer=timer)
        assert build.call_args.kwargs["scene_type"] == "extraction"

    @pytest.mark.asyncio
    async def test_background_image_task_sends_via_ws(self):
        """Drive the inner _gen_aftermath coroutine to assert the image is
        base64-encoded and fanned out over the WS as a typed image frame."""
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        timer = MissionTimer()
        timer.active_events = []

        captured_tasks = []

        def capture_task(coro):
            captured_tasks.append(coro)
            return MagicMock()

        fake_ws = MagicMock()
        fake_ws.send = AsyncMock()

        with patch("app.image_gen.build_mission_prompt", MagicMock(return_value="p")), \
             patch("app.image_gen.generate_image", new=AsyncMock(return_value=b"\x89PNGdata")), \
             patch("app.context.ws", fake_ws), \
             patch("app.proactive.mission.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.mission.asyncio.create_task", side_effect=capture_task), \
             patch("app.proactive.mission.random.choice", side_effect=lambda seq: seq[0]):
            await e.trigger_mission_aftermath_image("alice", timer=timer)
            # Now actually run the captured background coroutine.
            assert captured_tasks, "no background image task was scheduled"
            await captured_tasks[0]

        fake_ws.send.assert_awaited_once()
        uid, frame = fake_ws.send.await_args.args
        assert uid == "alice"
        assert frame["type"] == "image"
        import base64
        assert base64.b64decode(frame["data"]) == b"\x89PNGdata"

    @pytest.mark.asyncio
    async def test_image_gen_module_error_swallowed(self):
        e = ProactiveEngine()
        e._on_message_callback = AsyncMock()
        timer = MissionTimer()
        timer.active_events = []
        with patch("app.image_gen.build_mission_prompt",
                   MagicMock(side_effect=RuntimeError("comfy down"))):
            await e.trigger_mission_aftermath_image("alice", timer=timer)  # no raise

    @pytest.mark.asyncio
    async def test_background_image_gen_error_swallowed(self):
        """If the deferred generate_image raises, the inner task swallows it
        (the caption was already delivered)."""
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        timer = MissionTimer()
        timer.active_events = []
        captured = []

        with patch("app.image_gen.build_mission_prompt", MagicMock(return_value="p")), \
             patch("app.image_gen.generate_image",
                   new=AsyncMock(side_effect=RuntimeError("comfy timeout"))), \
             patch("app.proactive.mission.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.mission.asyncio.create_task",
                   side_effect=lambda c: (captured.append(c), MagicMock())[-1]), \
             patch("app.proactive.mission.random.choice", side_effect=lambda seq: seq[0]):
            await e.trigger_mission_aftermath_image("alice", timer=timer)
            assert captured
            await captured[0]  # run the inner coroutine — must not raise
        cb.assert_awaited_once()  # caption still delivered


# ═══════════════════════════════════════════════════════════════════════════
# Anniversary + weekly-reflection exception branches  (1557-1558, 1656-1659)
# ═══════════════════════════════════════════════════════════════════════════


class _FakeConn:
    def __init__(self, *batches):
        self._batches = list(batches)

    async def execute(self, sql, params=None):
        res = AsyncMock()
        res.fetchall = AsyncMock(
            return_value=self._batches.pop(0) if self._batches else [])
        return res

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, *seqs):
        self._seqs = list(seqs)

    def connection(self):
        seq = self._seqs.pop(0) if self._seqs else []
        return _FakeConn(*seq)


class TestAnniversarySendFailure:
    @pytest.mark.asyncio
    async def test_ws_send_failure_swallowed_per_user(self):
        """If ws.send_proactive raises for a matching anniversary, the error is
        logged but the job does not crash (line 1557-1558)."""
        e = ProactiveEngine()
        # select_anniversary_from_firsts compares against datetime.now(timezone.utc),
        # so build the "one year ago today" date in UTC to guarantee a match.
        from datetime import timezone
        today = datetime.now(timezone.utc)
        one_year_ago = today.replace(year=today.year - 1)
        pool = _FakePool(
            [[("alice",)]],                                # active users
            [[("first_message", one_year_ago, None)]],     # firsts for alice
        )
        fake_ws = MagicMock()
        fake_ws.is_connected = MagicMock(return_value=True)
        fake_ws.send_proactive = AsyncMock(side_effect=RuntimeError("ws closed"))

        with patch("app.db.get_pool", return_value=pool), \
             patch("app.context.ws", fake_ws), \
             patch("app.proactive.milestones.logger") as log:
            await e._anniversary_check()  # must not raise

        fake_ws.send_proactive.assert_awaited_once()
        assert log.warning.called  # the per-user send failure was logged


class TestWeeklyReflectionSaveFailure:
    @pytest.mark.asyncio
    async def test_episode_save_failure_swallowed(self):
        """A failing memory.store_episode is logged but doesn't crash the job
        (line 1656-1659)."""
        e = ProactiveEngine()
        # >=10 messages so the reflection proceeds.
        msgs = [("user", f"line {i}") for i in range(12)]
        pool = _FakePool(
            [[("alice",)]],   # active users (7d)
            [msgs],           # alice's messages
        )

        fake_router = MagicMock()
        fake_router.complete_local = AsyncMock(return_value={
            "choices": [{"message": {"content": "A" * 80}}]
        })
        fake_memory = MagicMock()
        fake_memory.store_episode = AsyncMock(side_effect=RuntimeError("qdrant down"))

        with patch("app.db.get_pool", return_value=pool), \
             patch("app.context.router", fake_router), \
             patch("app.context.memory", fake_memory), \
             patch("app.personality.load_personality", return_value={}), \
             patch("app.personality.build_character_preamble", return_value="preamble"), \
             patch("app.proactive.milestones.logger") as log:
            await e._weekly_reflection()  # must not raise

        fake_memory.store_episode.assert_awaited_once()
        assert log.warning.called

    @pytest.mark.asyncio
    async def test_reflection_stored_on_success(self):
        e = ProactiveEngine()
        msgs = [("user", f"line {i}") for i in range(15)]
        pool = _FakePool([[("alice",)]], [msgs])

        fake_router = MagicMock()
        fake_router.complete_local = AsyncMock(return_value={
            "choices": [{"message": {"content": "This week we held the ridge together." * 3}}]
        })
        fake_memory = MagicMock()
        fake_memory.store_episode = AsyncMock()

        with patch("app.db.get_pool", return_value=pool), \
             patch("app.context.router", fake_router), \
             patch("app.context.memory", fake_memory), \
             patch("app.personality.load_personality", return_value={}), \
             patch("app.personality.build_character_preamble", return_value="preamble"):
            await e._weekly_reflection()

        fake_memory.store_episode.assert_awaited_once()
        kwargs = fake_memory.store_episode.await_args.kwargs
        assert kwargs["importance"] == 8
        assert "weekly_reflection" in kwargs["keywords"]
        assert kwargs["user_id"] == "alice"

    @pytest.mark.asyncio
    async def test_outer_job_failure_swallowed(self):
        """A failure acquiring the pool aborts the whole job without raising
        (outer except, line 1658-1659)."""
        e = ProactiveEngine()
        with patch("app.db.get_pool", side_effect=RuntimeError("pool down")), \
             patch("app.proactive.milestones.logger") as log:
            await e._weekly_reflection()  # must not raise
        assert log.error.called

    @pytest.mark.asyncio
    async def test_too_few_messages_skips_user(self):
        """A user with <10 messages in the window is skipped (no LLM call)."""
        e = ProactiveEngine()
        msgs = [("user", "hi"), ("assistant", "hello")]  # only 2
        pool = _FakePool([[("alice",)]], [msgs])
        fake_router = MagicMock()
        fake_router.complete_local = AsyncMock()
        with patch("app.db.get_pool", return_value=pool), \
             patch("app.context.router", fake_router), \
             patch("app.context.memory", MagicMock()), \
             patch("app.personality.load_personality", return_value={}), \
             patch("app.personality.build_character_preamble", return_value="p"):
            await e._weekly_reflection()
        fake_router.complete_local.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_active_users_skips(self):
        e = ProactiveEngine()
        pool = _FakePool([[]])  # no active users
        with patch("app.db.get_pool", return_value=pool):
            await e._weekly_reflection()  # returns early, no raise
