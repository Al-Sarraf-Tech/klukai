"""Send gates for the proactive jobs round 1 did not cover.

Mission random events, the romance window, the daily challenge, and seasonal
greetings go through _can_send(now=<that job's clock>). The engine clock must
not replace it. Portrait backfill does not message him. Quiet-day check-in is
unchanged and may still ignore an unanswered line.
"""

from __future__ import annotations

import contextlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.proactive import MAX_PROACTIVE_PER_DAY, ProactiveEngine

_NOON = datetime(2026, 5, 17, 14, 0, 0)
_NIGHT = datetime(2026, 5, 17, 2, 0, 0)
_BEFORE_QUIET = datetime(2026, 5, 17, 22, 50, 0)
_QUIET = datetime(2026, 5, 17, 23, 10, 0)
# Later than a hold anchored at _NOON, and outside quiet hours. A bare
# _can_send() that reads this instead of the job clock would both clear the
# hold and allow the send.
_ENGINE_LATER = datetime(2026, 11, 24, 15, 0, 0)
_CHRISTMAS_NOON = datetime(2026, 12, 25, 12, 0, 0)
_CHRISTMAS_NIGHT = datetime(2026, 12, 25, 2, 0, 0)

_CHALLENGES = {
    "daily_challenges": {
        "challenges": [{"type": "creative", "prompt": "Write a haiku."}]
    }
}
_SEASONAL = {
    "seasonal_events": {
        "christmas": {
            "month": 12,
            "day": 25,
            "min_affection": 0,
            "messages": ["Merry Christmas, Commander."],
        }
    }
}


@contextlib.contextmanager
def _job_clock(module: str, when: datetime, *, engine: datetime | None = _ENGINE_LATER):
    """Freeze the job module's clock, and optionally a different engine clock."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch(f"app.proactive.{module}.now_local", return_value=when))
        if engine is not None:
            stack.enter_context(
                patch("app.proactive.engine.now_local", return_value=engine)
            )
        yield


def _engine() -> ProactiveEngine:
    engine = ProactiveEngine()
    engine._affection_level = 5
    engine._on_message_callback = AsyncMock()
    engine._last_proactive_answered = True
    return engine


def _mission_engine() -> ProactiveEngine:
    engine = _engine()
    timer = MagicMock()
    timer.active = True
    timer.mission_description = "Hold the ridge"
    timer.started_at = 1.0
    timer.update_count = 2
    timer.active_events = []
    engine._mission_timer = timer
    return engine


def _romance_engine() -> ProactiveEngine:
    engine = _engine()
    engine._affection_level = 3
    engine._user_messaged_today = True
    engine._romance_delivered_today = False
    return engine


class TestMissionRandomEventGate:
    def _run(self, engine: ProactiveEngine, when: datetime, *, engine_clock=_ENGINE_LATER):
        gen = AsyncMock(return_value="Contact on the ridge.")
        stack = contextlib.ExitStack()
        stack.enter_context(_job_clock("mission", when, engine=engine_clock))
        stack.enter_context(patch("app.proactive.mission.random.random", return_value=0.0))
        stack.enter_context(patch("app.fact_extractor.generate_mission_update", gen))
        return stack, gen

    @pytest.mark.asyncio
    async def test_quiet_hours_block_before_the_model(self):
        engine = _mission_engine()
        stack, gen = self._run(engine, _NIGHT)
        with stack:
            await engine._mission_random_event()
        gen.assert_not_awaited()
        engine._on_message_callback.assert_not_awaited()
        assert engine._proactive_count_today == 0
        assert engine._random_events_today == 0

    @pytest.mark.asyncio
    async def test_goodbye_uses_the_job_clock(self):
        engine = _mission_engine()
        engine._goodbye_hold_until = _NOON + timedelta(minutes=30)
        stack, gen = self._run(engine, _NOON)
        with stack:
            await engine._mission_random_event()
        gen.assert_not_awaited()
        engine._on_message_callback.assert_not_awaited()
        assert engine._goodbye_hold_until == _NOON + timedelta(minutes=30)

    @pytest.mark.asyncio
    async def test_mute_blocks(self):
        engine = _mission_engine()
        engine._muted_until = datetime(2099, 1, 1)
        stack, gen = self._run(engine, _NOON, engine_clock=_NOON)
        with stack:
            await engine._mission_random_event()
        gen.assert_not_awaited()
        engine._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shared_cap_blocks_without_raising_it(self):
        engine = _mission_engine()
        engine._proactive_count_today = MAX_PROACTIVE_PER_DAY
        stack, gen = self._run(engine, _NOON, engine_clock=_NOON)
        with stack:
            await engine._mission_random_event()
        gen.assert_not_awaited()
        engine._on_message_callback.assert_not_awaited()
        assert engine._proactive_count_today == MAX_PROACTIVE_PER_DAY

    @pytest.mark.asyncio
    async def test_unanswered_line_blocks(self):
        engine = _mission_engine()
        engine._last_proactive_answered = False
        stack, gen = self._run(engine, _NOON, engine_clock=_NOON)
        with stack:
            await engine._mission_random_event()
        gen.assert_not_awaited()
        engine._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_open_gate_counts_and_marks_unanswered(self):
        engine = _mission_engine()
        # Engine clock is inside quiet hours. The job clock is noon, and that
        # is the instant the gate must use.
        stack, gen = self._run(engine, _NOON, engine_clock=_NIGHT)
        with stack:
            await engine._mission_random_event()
        gen.assert_awaited_once()
        engine._on_message_callback.assert_awaited_once_with("Contact on the ridge.")
        assert engine._proactive_count_today == 1
        assert engine._random_events_today == 1
        assert engine._last_proactive_answered is False

    @pytest.mark.asyncio
    async def test_model_that_crosses_quiet_hours_is_not_sent(self):
        engine = _mission_engine()
        times = iter((_BEFORE_QUIET, _QUIET))
        gen = AsyncMock(return_value="Contact on the ridge.")
        with (
            patch("app.proactive.mission.now_local", side_effect=lambda: next(times)),
            patch("app.proactive.engine.now_local", return_value=_NOON),
            patch("app.proactive.mission.random.random", return_value=0.0),
            patch("app.fact_extractor.generate_mission_update", gen),
        ):
            await engine._mission_random_event()
        gen.assert_awaited_once()
        engine._on_message_callback.assert_not_awaited()
        assert engine._proactive_count_today == 0
        assert engine._random_events_today == 0
        assert engine._last_proactive_answered is True


class TestRomanceWindowGate:
    @pytest.mark.asyncio
    async def test_quiet_hours_block_before_the_delay(self):
        engine = _romance_engine()
        with (
            _job_clock("events", _NIGHT),
            patch("app.proactive.events.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            await engine._romance_window()
        sleep.assert_not_awaited()
        engine._on_message_callback.assert_not_awaited()
        assert engine._romance_delivered_today is False
        assert engine._proactive_count_today == 0

    @pytest.mark.asyncio
    async def test_goodbye_uses_the_job_clock(self):
        engine = _romance_engine()
        engine._goodbye_hold_until = _NOON + timedelta(minutes=30)
        with (
            _job_clock("events", _NOON),
            patch("app.proactive.events.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            await engine._romance_window()
        sleep.assert_not_awaited()
        engine._on_message_callback.assert_not_awaited()
        assert engine._goodbye_hold_until == _NOON + timedelta(minutes=30)
        assert engine._romance_delivered_today is False

    @pytest.mark.asyncio
    async def test_shared_cap_blocks(self):
        engine = _romance_engine()
        engine._proactive_count_today = MAX_PROACTIVE_PER_DAY
        with (
            _job_clock("events", _NOON, engine=_NOON),
            patch("app.proactive.events.asyncio.sleep", new=AsyncMock()),
        ):
            await engine._romance_window()
        engine._on_message_callback.assert_not_awaited()
        assert engine._proactive_count_today == MAX_PROACTIVE_PER_DAY
        assert engine._romance_delivered_today is False

    @pytest.mark.asyncio
    async def test_unanswered_blocks_before_the_delay(self):
        engine = _romance_engine()
        engine._last_proactive_answered = False
        with (
            _job_clock("events", _NOON, engine=_NOON),
            patch("app.proactive.events.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            await engine._romance_window()
        sleep.assert_not_awaited()
        engine._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delay_that_crosses_quiet_hours_does_not_send(self):
        engine = _romance_engine()
        times = iter((_BEFORE_QUIET, _QUIET))
        with (
            patch("app.proactive.events.now_local", side_effect=lambda: next(times)),
            patch("app.proactive.engine.now_local", return_value=_NOON),
            patch("app.proactive.events.random.uniform", return_value=0),
            patch("app.proactive.events.asyncio.sleep", new=AsyncMock()),
        ):
            await engine._romance_window()
        engine._on_message_callback.assert_not_awaited()
        assert engine._romance_delivered_today is False
        assert engine._proactive_count_today == 0

    @pytest.mark.asyncio
    async def test_cap_filled_during_the_delay_does_not_send(self):
        engine = _romance_engine()

        async def fill_cap(*_a, **_k):
            engine._proactive_count_today = MAX_PROACTIVE_PER_DAY

        with (
            _job_clock("events", _NOON, engine=_NOON),
            patch("app.proactive.events.random.uniform", return_value=0),
            patch("app.proactive.events.asyncio.sleep", side_effect=fill_cap),
        ):
            await engine._romance_window()
        engine._on_message_callback.assert_not_awaited()
        assert engine._proactive_count_today == MAX_PROACTIVE_PER_DAY
        assert engine._romance_delivered_today is False

    @pytest.mark.asyncio
    async def test_unanswered_during_the_delay_does_not_send(self):
        engine = _romance_engine()

        async def go_unanswered(*_a, **_k):
            engine._last_proactive_answered = False

        with (
            _job_clock("events", _NOON, engine=_NOON),
            patch("app.proactive.events.random.uniform", return_value=0),
            patch("app.proactive.events.asyncio.sleep", side_effect=go_unanswered),
        ):
            await engine._romance_window()
        engine._on_message_callback.assert_not_awaited()
        assert engine._romance_delivered_today is False

    @pytest.mark.asyncio
    async def test_open_gate_sends_on_the_job_clock(self):
        engine = _romance_engine()
        with (
            _job_clock("events", _NOON, engine=_NIGHT),
            patch("app.proactive.events.random.uniform", return_value=0),
            patch("app.proactive.events.asyncio.sleep", new=AsyncMock()),
            patch("app.proactive.events.publish_event", new=AsyncMock()),
        ):
            await engine._romance_window()
        engine._on_message_callback.assert_awaited_once()
        assert engine._proactive_count_today == 1
        assert engine._last_proactive_answered is False
        assert engine._romance_delivered_today is True


class TestDailyChallengeGate:
    @pytest.mark.asyncio
    async def test_issues_once_and_leaves_the_line_unanswered(self):
        engine = _engine()
        engine._affection_level = 5
        with (
            patch("app.proactive.engine.now_local", return_value=_NOON),
            patch("app.personality.load_personality", return_value=_CHALLENGES),
        ):
            await engine._daily_challenge()
            await engine._daily_challenge()
        engine._on_message_callback.assert_awaited_once_with("Write a haiku.")
        assert engine._proactive_count_today == 1
        assert engine._last_proactive_answered is False

    @pytest.mark.asyncio
    async def test_quiet_hours_do_not_increment(self):
        engine = _engine()
        with (
            patch("app.proactive.engine.now_local", return_value=_NIGHT),
            patch("app.personality.load_personality", return_value=_CHALLENGES),
        ):
            await engine._daily_challenge()
        engine._on_message_callback.assert_not_awaited()
        assert engine._proactive_count_today == 0
        assert engine._last_proactive_answered is True

    @pytest.mark.asyncio
    async def test_goodbye_mute_cap_and_unanswered_block(self):
        engine = _engine()
        engine._goodbye_hold_until = _NOON + timedelta(minutes=30)
        with (
            patch("app.proactive.engine.now_local", return_value=_NOON),
            patch("app.personality.load_personality", return_value=_CHALLENGES),
        ):
            await engine._daily_challenge()
        engine._on_message_callback.assert_not_awaited()
        assert engine._goodbye_hold_until == _NOON + timedelta(minutes=30)
        assert engine._proactive_count_today == 0

        engine._goodbye_hold_until = None
        engine._muted_until = datetime(2099, 1, 1)
        with (
            patch("app.proactive.engine.now_local", return_value=_NOON),
            patch("app.personality.load_personality", return_value=_CHALLENGES),
        ):
            await engine._daily_challenge()
        engine._on_message_callback.assert_not_awaited()

        engine._muted_until = None
        engine._proactive_count_today = MAX_PROACTIVE_PER_DAY
        with (
            patch("app.proactive.engine.now_local", return_value=_NOON),
            patch("app.personality.load_personality", return_value=_CHALLENGES),
        ):
            await engine._daily_challenge()
        assert engine._proactive_count_today == MAX_PROACTIVE_PER_DAY
        engine._on_message_callback.assert_not_awaited()

        engine._proactive_count_today = 0
        engine._last_proactive_answered = False
        with (
            patch("app.proactive.engine.now_local", return_value=_NOON),
            patch("app.personality.load_personality", return_value=_CHALLENGES),
        ):
            await engine._daily_challenge()
        engine._on_message_callback.assert_not_awaited()
        assert engine._proactive_count_today == 0


class TestSeasonalGate:
    @pytest.mark.asyncio
    async def test_night_holiday_does_not_send(self):
        engine = _engine()
        with (
            _job_clock("events", _CHRISTMAS_NIGHT, engine=_NOON),
            patch("app.personality.load_personality", return_value=_SEASONAL),
        ):
            await engine._seasonal_check()
        engine._on_message_callback.assert_not_awaited()
        assert engine._seasonal_delivered == {}
        assert engine._proactive_count_today == 0

    @pytest.mark.asyncio
    async def test_goodbye_cap_and_unanswered_block(self):
        engine = _engine()
        hold = _CHRISTMAS_NOON + timedelta(hours=1)
        engine._goodbye_hold_until = hold
        with (
            _job_clock("events", _CHRISTMAS_NOON),
            patch("app.personality.load_personality", return_value=_SEASONAL),
        ):
            await engine._seasonal_check()
        engine._on_message_callback.assert_not_awaited()
        assert engine._goodbye_hold_until == hold

        engine._goodbye_hold_until = None
        engine._proactive_count_today = MAX_PROACTIVE_PER_DAY
        with (
            _job_clock("events", _CHRISTMAS_NOON, engine=_CHRISTMAS_NOON),
            patch("app.personality.load_personality", return_value=_SEASONAL),
        ):
            await engine._seasonal_check()
        engine._on_message_callback.assert_not_awaited()
        assert engine._proactive_count_today == MAX_PROACTIVE_PER_DAY

        engine._proactive_count_today = 0
        engine._last_proactive_answered = False
        with (
            _job_clock("events", _CHRISTMAS_NOON, engine=_CHRISTMAS_NOON),
            patch("app.personality.load_personality", return_value=_SEASONAL),
        ):
            await engine._seasonal_check()
        engine._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_noon_holiday_uses_the_job_clock(self):
        engine = _engine()
        with (
            _job_clock("events", _CHRISTMAS_NOON, engine=_NIGHT),
            patch("app.personality.load_personality", return_value=_SEASONAL),
            patch("app.proactive.events.publish_event", new=AsyncMock()),
        ):
            await engine._seasonal_check()
        engine._on_message_callback.assert_awaited_once_with(
            "Merry Christmas, Commander."
        )
        assert engine._proactive_count_today == 1
        assert engine._last_proactive_answered is False
        assert engine._seasonal_delivered.get("christmas:2026-12-25") is True


class TestPortraitBackfillStaysSilent:
    @pytest.mark.asyncio
    async def test_return_value_is_not_a_message(self):
        engine = _engine()
        tick = AsyncMock(return_value="a portrait is ready")
        with patch("app.portrait.backfill_tick", tick):
            await engine._portrait_backfill()
        tick.assert_awaited_once()
        engine._on_message_callback.assert_not_awaited()
        assert engine._proactive_count_today == 0
