"""Timezone + daily-reset state correctness for the proactive engine.

The container runs UTC but the Commander lives in America/Chicago. Every
scheduled job, quiet-hour window, and time-of-day context block must be
expressed in the Commander's wall clock via the single ``now_local()`` helper
(DST handled by zoneinfo — no hand-converted CST offsets).

Also covers the daily-reset state bugs: ``_reset_daily`` must clear the
``_last_proactive_answered`` latch, and ``_quiet_day_check`` must evaluate
independently of earlier same-day proactive sends.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger

from app.proactive import ProactiveEngine
from app.proactive.state import LOCAL_TZ, now_local

# 2026-05-17 is a Sunday; 15:00 is a safe send window (not quiet hours).
_SUNDAY_3PM = datetime(2026, 5, 17, 15, 0, 0)

_NOW_LOCAL_TARGETS = (
    "app.proactive.engine.now_local",
    "app.proactive.events.now_local",
    "app.proactive.patterns.now_local",
)


def _trigger_fields(job) -> dict[str, str]:
    return {f.name: str(f) for f in job.trigger.fields}


def _quiet_sunday_cache(at: datetime) -> dict:
    return {
        "patterns:alice": (at, {
            "quiet_on_sunday": {
                "type": "quiet_day", "day": "sunday", "dow": 0,
                "confidence": 0.95, "user_msgs": 0, "overall_avg": 10.0,
            },
        })
    }


# ═══════════════════════════════════════════════════════════════════════════
# now_local() helper
# ═══════════════════════════════════════════════════════════════════════════


class TestNowLocalHelper:
    def test_local_tz_is_chicago(self):
        assert str(LOCAL_TZ) == "America/Chicago"

    def test_now_local_returns_naive_chicago_wall_clock(self):
        wall = datetime.now(ZoneInfo("America/Chicago")).replace(tzinfo=None)
        got = now_local()
        assert got.tzinfo is None
        assert abs((got - wall).total_seconds()) < 5


# ═══════════════════════════════════════════════════════════════════════════
# Scheduler timezone — every cron job runs on the Commander's wall clock
# ═══════════════════════════════════════════════════════════════════════════


class TestSchedulerTimezone:
    @pytest.mark.asyncio
    async def test_all_cron_jobs_scheduled_in_chicago(self):
        e = ProactiveEngine()
        try:
            e.start()
            crons = [
                j for j in e._scheduler.get_jobs()
                if isinstance(j.trigger, CronTrigger)
            ]
            assert crons, "expected cron jobs to be registered"
            for job in crons:
                assert str(job.trigger.timezone) == "America/Chicago", (
                    f"job {job.id} scheduled in {job.trigger.timezone}"
                )
        finally:
            e.stop()

    @pytest.mark.asyncio
    async def test_job_hours_are_local_wall_clock(self):
        """Hours are expressed directly in America/Chicago — no hand-converted
        UTC offsets (which silently broke during CDT)."""
        e = ProactiveEngine()
        try:
            e.start()
            jobs = {j.id: j for j in e._scheduler.get_jobs()}

            # Morning briefing fires 08:00 Chicago, not 08:00 UTC (02-03:00 local).
            assert _trigger_fields(jobs["morning_checkin"])["hour"] == "8"
            # Late-night dreams fire 01-04 Chicago, not in the local evening.
            assert _trigger_fields(jobs["dream_event"])["hour"] == "1-4"
            # Romance window: 20:30 local — previously hand-converted to 02:30 UTC.
            rf = _trigger_fields(jobs["romance_window"])
            assert (rf["hour"], rf["minute"]) == ("20", "30")
            # Weekly reflection: Sunday 21:00 local — previously Monday 03:00 UTC.
            wf = _trigger_fields(jobs["weekly_reflection"])
            assert (wf["day_of_week"], wf["hour"]) == ("sun", "21")
            # Anniversary check runs just before the 08:00 morning greeting.
            af = _trigger_fields(jobs["anniversary_check"])
            assert (af["hour"], af["minute"]) == ("7", "58")
            # Daily reset at local midnight.
            assert _trigger_fields(jobs["daily_reset"])["hour"] == "0"
        finally:
            e.stop()


# ═══════════════════════════════════════════════════════════════════════════
# _can_send quiet hours evaluate in local time
# ═══════════════════════════════════════════════════════════════════════════


class TestCanSendUsesLocalClock:
    def _engine(self):
        e = ProactiveEngine()
        e._muted_until = None
        e._proactive_count_today = 0
        e._last_proactive_answered = True
        return e

    def test_blocked_at_5am_local(self):
        e = self._engine()
        with patch("app.proactive.engine.now_local",
                   return_value=datetime(2026, 5, 17, 5, 0, 0)):
            assert e._can_send() is False

    def test_blocked_at_2330_local(self):
        e = self._engine()
        with patch("app.proactive.engine.now_local",
                   return_value=datetime(2026, 5, 17, 23, 30, 0)):
            assert e._can_send() is False

    def test_allowed_at_2pm_local(self):
        e = self._engine()
        with patch("app.proactive.engine.now_local",
                   return_value=datetime(2026, 5, 17, 14, 0, 0)):
            assert e._can_send() is True


# ═══════════════════════════════════════════════════════════════════════════
# Time-of-day context blocks use local time
# ═══════════════════════════════════════════════════════════════════════════


class TestMoodContextUsesLocalClock:
    def test_context_block_morning_in_chicago(self):
        from app.personality.moods import build_context_block
        with patch("app.personality.moods.now_local",
                   return_value=datetime(2026, 5, 17, 8, 30, 0)):
            block = build_context_block()
        assert "0830 hours" in block
        assert "morning operational window" in block

    def test_context_block_late_watch_in_chicago(self):
        from app.personality.moods import build_context_block
        with patch("app.personality.moods.now_local",
                   return_value=datetime(2026, 5, 17, 23, 30, 0)):
            block = build_context_block(affection_level=3)
        assert "2330 hours" in block
        assert "late-night watch" in block


class TestReflectionUsesLocalHour:
    @pytest.mark.asyncio
    async def test_classify_receives_chicago_hour(self):
        """The return-greeting classifier gets the Commander's wall-clock hour,
        not the server's UTC hour."""
        from app.reflect_helpers import _maybe_reflect_on_return

        captured: dict = {}

        def fake_classify(*, hours_away, local_hour, min_hours, max_hours):
            captured["local_hour"] = local_hour
            return "silent"  # short-circuit before any LLM work

        last_at = datetime.now(timezone.utc) - timedelta(hours=24)

        class _Conn:
            def __init__(self):
                self._calls = 0

            async def execute(self, sql, params=None):
                self._calls += 1
                res = AsyncMock()
                if "MAX(created_at)" in sql:
                    res.fetchone = AsyncMock(return_value=(last_at,))
                elif "FROM companion_persistent_state" in sql:
                    res.fetchone = AsyncMock(return_value=("composed",))
                else:
                    res.fetchall = AsyncMock(
                        return_value=[("user", "hello"), ("assistant", "hi")]
                    )
                return res

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

        pool = MagicMock()
        pool.connection = lambda: _Conn()

        with patch("app.db.get_pool", return_value=pool), \
             patch("app.character_behaviors.classify_return_greeting",
                   side_effect=fake_classify), \
             patch("app.reflect_helpers.now_local",
                   return_value=datetime(2026, 5, 17, 22, 15, 0)):
            await _maybe_reflect_on_return("jalsarraf")

        assert captured["local_hour"] == 22


# ═══════════════════════════════════════════════════════════════════════════
# Daily reset clears the unanswered-proactive latch
# ═══════════════════════════════════════════════════════════════════════════


class TestResetDailyClearsAnsweredLatch:
    @pytest.mark.asyncio
    async def test_reset_restores_last_proactive_answered(self):
        """One unanswered proactive must not silence the engine on later days:
        the midnight reset gives her a fresh opening line."""
        e = ProactiveEngine()
        e._last_proactive_answered = False
        await e._reset_daily()
        assert e._last_proactive_answered is True

    @pytest.mark.asyncio
    async def test_can_send_allowed_again_after_reset(self):
        e = ProactiveEngine()
        e._muted_until = None
        e._last_proactive_answered = False
        await e._reset_daily()
        with patch("app.proactive.engine.now_local",
                   return_value=datetime(2026, 5, 18, 14, 0, 0)):
            assert e._can_send() is True


# ═══════════════════════════════════════════════════════════════════════════
# Quiet-day check is independent of earlier same-day proactive sends
# ═══════════════════════════════════════════════════════════════════════════


class TestQuietDayIndependentOfSameDaySends:
    def _engine(self):
        e = ProactiveEngine()
        e._on_message_callback = AsyncMock()
        e._affection_level = 3
        e._proactive_count_today = 1  # e.g. the unanswered morning check-in
        e._last_proactive_answered = False  # quiet day: of course he hasn't replied
        e._pattern_cache = _quiet_sunday_cache(_SUNDAY_3PM)
        return e

    def _patch_now(self, value=_SUNDAY_3PM):
        import contextlib
        stack = contextlib.ExitStack()
        for target in _NOW_LOCAL_TARGETS:
            stack.enter_context(patch(target, return_value=value))
        return stack

    @pytest.mark.asyncio
    async def test_fires_despite_unanswered_earlier_proactive(self):
        e = self._engine()
        with self._patch_now(), \
             patch("app.proactive.events.publish_event", new=AsyncMock()):
            await e._quiet_day_check("alice")
        e._on_message_callback.assert_awaited_once()
        assert e._quiet_day_delivered_today is True

    @pytest.mark.asyncio
    async def test_still_respects_daily_cap(self):
        from app.proactive import MAX_PROACTIVE_PER_DAY
        e = self._engine()
        e._proactive_count_today = MAX_PROACTIVE_PER_DAY
        with self._patch_now():
            await e._quiet_day_check("alice")
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_still_respects_mute(self):
        e = self._engine()
        e._muted_until = datetime(2099, 1, 1)
        with self._patch_now():
            await e._quiet_day_check("alice")
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_still_respects_quiet_hours(self):
        e = self._engine()
        late = datetime(2026, 5, 17, 23, 30, 0)
        e._pattern_cache = _quiet_sunday_cache(late)
        with self._patch_now(late):
            await e._quiet_day_check("alice")
        e._on_message_callback.assert_not_awaited()
