"""Tests for app/proactive/durability.py — surviving missed cron fires.

The failure this guards against is invisible by construction: with an in-memory
jobstore, a fire time that passes while the process is down never existed as a
job, so APScheduler neither runs it nor logs a misfire. A deploy at 07:58
silently costs the 08:00 morning check-in.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.proactive import durability as dur  # noqa: E402


UTC = timezone.utc


def _cron(**fields):
    from apscheduler.triggers.cron import CronTrigger
    return CronTrigger(timezone=UTC, **fields)


# ─────────────────────────────────────────────────────────────────────────
# Allowlist policy
# ─────────────────────────────────────────────────────────────────────────


class TestCatchUpPolicy:
    def test_greetings_and_summaries_are_replayable(self):
        for job in ("morning_checkin", "daily_recap", "evening_checkin",
                    "daily_challenge"):
            assert job in dur.CATCH_UP_WINDOWS

    def test_dated_occasions_are_replayable(self):
        for job in ("anniversary_check", "seasonal_check", "weekly_reflection"):
            assert job in dur.CATCH_UP_WINDOWS

    def test_daily_reset_is_never_replayed(self):
        """Replaying the midnight reset at 09:00 would wipe counters the day
        has already accrued."""
        assert "daily_reset" in dur.NEVER_CATCH_UP
        assert "daily_reset" not in dur.CATCH_UP_WINDOWS

    def test_ambient_and_time_bound_jobs_are_never_replayed(self):
        for job in ("random_event", "idle_check", "dream_event",
                    "spontaneous_art", "romance_window", "memory_recall"):
            assert job in dur.NEVER_CATCH_UP
            assert job not in dur.CATCH_UP_WINDOWS

    def test_the_two_lists_never_overlap(self):
        assert not (set(dur.CATCH_UP_WINDOWS) & dur.NEVER_CATCH_UP)

    def test_misfire_grace_is_not_the_one_second_default(self):
        """One second is short enough that an image render drops the job."""
        assert dur.MISFIRE_GRACE_SECONDS >= 60


# ─────────────────────────────────────────────────────────────────────────
# previous_fire_time
# ─────────────────────────────────────────────────────────────────────────


class TestPreviousFireTime:
    def test_finds_the_most_recent_daily_fire(self):
        now = datetime(2026, 8, 6, 11, 0, tzinfo=UTC)
        got = dur.previous_fire_time(
            _cron(hour=8, minute=0), now, now - timedelta(hours=6)
        )
        assert got == datetime(2026, 8, 6, 8, 0, tzinfo=UTC)

    def test_picks_the_latest_of_several_in_window(self):
        now = datetime(2026, 8, 6, 19, 0, tzinfo=UTC)
        got = dur.previous_fire_time(
            _cron(hour="10,14,18", minute=0), now, now - timedelta(hours=12)
        )
        assert got == datetime(2026, 8, 6, 18, 0, tzinfo=UTC)

    def test_none_when_never_due_in_window(self):
        now = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)
        got = dur.previous_fire_time(
            _cron(hour=22, minute=0), now, now - timedelta(hours=2)
        )
        assert got is None

    def test_excludes_a_fire_time_still_in_the_future(self):
        now = datetime(2026, 8, 6, 7, 59, tzinfo=UTC)
        got = dur.previous_fire_time(
            _cron(hour=8, minute=0), now, now - timedelta(hours=4)
        )
        assert got is None

    def test_weekly_trigger(self):
        # 2026-08-06 is a Thursday; the previous Sunday is 2026-08-02.
        now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        got = dur.previous_fire_time(
            _cron(day_of_week="sun", hour=21, minute=0), now, now - timedelta(days=6)
        )
        assert got == datetime(2026, 8, 2, 21, 0, tzinfo=UTC)

    def test_a_broken_trigger_does_not_raise(self):
        trigger = MagicMock()
        trigger.get_next_fire_time.side_effect = RuntimeError("bad trigger")
        now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        assert dur.previous_fire_time(trigger, now, now - timedelta(hours=1)) is None

    def test_a_non_advancing_trigger_terminates(self):
        """A trigger that keeps returning the same time must not spin forever."""
        stuck = datetime(2026, 8, 6, 10, 0, tzinfo=UTC)
        trigger = MagicMock()
        trigger.get_next_fire_time.return_value = stuck
        now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        assert dur.previous_fire_time(trigger, now, now - timedelta(hours=4)) == stuck


# ─────────────────────────────────────────────────────────────────────────
# missed_jobs
# ─────────────────────────────────────────────────────────────────────────


class TestMissedJobs:
    def test_detects_a_job_missed_while_down(self):
        """The core case: deploy at 07:58, back at 08:05, 08:00 never ran."""
        now = datetime(2026, 8, 6, 8, 5, tzinfo=UTC)
        jobs = [("morning_checkin", _cron(hour=8, minute=0))]
        last = {"morning_checkin": datetime(2026, 8, 5, 8, 0, tzinfo=UTC)}

        missed = dur.missed_jobs(jobs, last, now=now)

        assert [j for j, _ in missed] == ["morning_checkin"]
        assert missed[0][1] == datetime(2026, 8, 6, 8, 0, tzinfo=UTC)

    def test_a_job_that_already_ran_is_not_replayed(self):
        now = datetime(2026, 8, 6, 8, 5, tzinfo=UTC)
        jobs = [("morning_checkin", _cron(hour=8, minute=0))]
        last = {"morning_checkin": datetime(2026, 8, 6, 8, 0, tzinfo=UTC)}

        assert dur.missed_jobs(jobs, last, now=now) == []

    def test_stale_misses_fall_outside_the_window(self):
        """A three-day-old morning briefing is noise, not care."""
        now = datetime(2026, 8, 6, 20, 0, tzinfo=UTC)
        jobs = [("morning_checkin", _cron(hour=8, minute=0))]
        last = {"morning_checkin": datetime(2026, 8, 3, 8, 0, tzinfo=UTC)}

        assert dur.missed_jobs(jobs, last, now=now) == []

    def test_non_allowlisted_jobs_are_ignored(self):
        now = datetime(2026, 8, 6, 8, 5, tzinfo=UTC)
        jobs = [("daily_reset", _cron(hour=0, minute=0)),
                ("random_event", _cron(hour="9-23", minute="15,45"))]

        assert dur.missed_jobs(jobs, {}, now=now) == []

    def test_fresh_database_only_replays_within_the_window(self):
        """No history must not mean 'replay a day of greetings at once'."""
        now = datetime(2026, 8, 6, 8, 5, tzinfo=UTC)
        jobs = [
            ("morning_checkin", _cron(hour=8, minute=0)),   # due 08:00, in window
            ("evening_checkin", _cron(hour=22, minute=0)),  # last due yesterday 22:00
        ]

        missed = [j for j, _ in dur.missed_jobs(jobs, {}, now=now)]

        assert missed == ["morning_checkin"]

    def test_results_are_ordered_by_due_time(self):
        now = datetime(2026, 8, 6, 11, 0, tzinfo=UTC)
        jobs = [
            ("daily_challenge", _cron(hour=9, minute=0)),
            ("anniversary_check", _cron(hour=7, minute=58)),
            ("morning_checkin", _cron(hour=8, minute=0)),
        ]

        missed = [j for j, _ in dur.missed_jobs(jobs, {}, now=now)]

        assert missed == ["anniversary_check", "morning_checkin", "daily_challenge"]


# ─────────────────────────────────────────────────────────────────────────
# run_catch_up
# ─────────────────────────────────────────────────────────────────────────


class TestRunCatchUp:
    @pytest.mark.asyncio
    async def test_replays_the_missed_job(self):
        now = datetime(2026, 8, 6, 8, 5, tzinfo=UTC)
        fn = AsyncMock()
        jobs = [("morning_checkin", _cron(hour=8, minute=0), fn)]

        with patch.object(dur, "load_last_fired", AsyncMock(return_value={})):
            with patch.object(dur, "record_fire", AsyncMock()) as rec:
                ran = await dur.run_catch_up(jobs, now=now)

        assert ran == ["morning_checkin"]
        fn.assert_awaited_once()
        assert rec.await_args.kwargs["status"] == "caught_up"

    @pytest.mark.asyncio
    async def test_nothing_missed_runs_nothing(self):
        now = datetime(2026, 8, 6, 8, 5, tzinfo=UTC)
        fn = AsyncMock()
        jobs = [("morning_checkin", _cron(hour=8, minute=0), fn)]
        last = {"morning_checkin": datetime(2026, 8, 6, 8, 0, tzinfo=UTC)}

        with patch.object(dur, "load_last_fired", AsyncMock(return_value=last)):
            ran = await dur.run_catch_up(jobs, now=now)

        assert ran == []
        fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_failing_job_does_not_stop_the_others(self):
        now = datetime(2026, 8, 6, 11, 0, tzinfo=UTC)
        bad = AsyncMock(side_effect=RuntimeError("boom"))
        good = AsyncMock()
        jobs = [
            ("anniversary_check", _cron(hour=7, minute=58), bad),
            ("morning_checkin", _cron(hour=8, minute=0), good),
        ]

        with patch.object(dur, "load_last_fired", AsyncMock(return_value={})):
            with patch.object(dur, "record_fire", AsyncMock()):
                ran = await dur.run_catch_up(jobs, now=now)

        good.assert_awaited_once()
        assert ran == ["morning_checkin"]

    @pytest.mark.asyncio
    async def test_a_broken_history_lookup_replays_nothing(self):
        """Failing safe means running nothing, not running everything."""
        now = datetime(2026, 8, 6, 8, 5, tzinfo=UTC)
        fn = AsyncMock()
        jobs = [("morning_checkin", _cron(hour=8, minute=0), fn)]

        with patch.object(dur, "load_last_fired",
                          AsyncMock(side_effect=RuntimeError("db down"))):
            ran = await dur.run_catch_up(jobs, now=now)

        assert ran == []
        fn.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────
# persistence helpers
# ─────────────────────────────────────────────────────────────────────────


def _conn_ctx(conn):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm():
        yield conn

    return _cm


class TestPersistence:
    @pytest.mark.asyncio
    async def test_record_fire_upserts(self):
        conn = MagicMock()
        conn.execute = AsyncMock()
        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            await dur.record_fire("morning_checkin")

        sql = conn.execute.await_args.args[0]
        assert "companion_job_runs" in sql
        assert "ON CONFLICT" in sql

    @pytest.mark.asyncio
    async def test_record_fire_never_raises(self):
        """Bookkeeping must not be able to break the job it is recording."""
        with patch("app.db.get_conn_autocommit", side_effect=RuntimeError("down")):
            await dur.record_fire("morning_checkin")  # must not raise

    @pytest.mark.asyncio
    async def test_load_last_fired_makes_naive_rows_aware(self):
        result = MagicMock()
        result.fetchall = AsyncMock(
            return_value=[("morning_checkin", datetime(2026, 8, 6, 8, 0))]
        )
        conn = MagicMock()
        conn.execute = AsyncMock(return_value=result)

        with patch("app.db.get_conn", _conn_ctx(conn)):
            out = await dur.load_last_fired()

        assert out["morning_checkin"].tzinfo is not None

    @pytest.mark.asyncio
    async def test_load_last_fired_skips_null_rows(self):
        result = MagicMock()
        result.fetchall = AsyncMock(return_value=[("a", None), ("b", datetime.now(UTC))])
        conn = MagicMock()
        conn.execute = AsyncMock(return_value=result)

        with patch("app.db.get_conn", _conn_ctx(conn)):
            out = await dur.load_last_fired()

        assert "a" not in out and "b" in out

    @pytest.mark.asyncio
    async def test_load_last_fired_is_empty_on_failure(self):
        with patch("app.db.get_conn", side_effect=RuntimeError("down")):
            assert await dur.load_last_fired() == {}


class TestTriggerWalkGuards:
    def test_step_ceiling_stops_a_runaway_trigger(self):
        """A trigger that advances by a microsecond must not walk forever."""
        start = datetime(2026, 8, 6, 0, 0, tzinfo=UTC)
        now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)

        class Creeping:
            def get_next_fire_time(self, previous, when):
                base = previous or when
                return base + timedelta(microseconds=1)

        with patch.object(dur, "_MAX_TRIGGER_STEPS", 25):
            got = dur.previous_fire_time(Creeping(), now, start)

        assert got is not None
        assert got < now

    def test_a_trigger_that_raises_mid_walk_keeps_what_it_found(self):
        first = datetime(2026, 8, 6, 8, 0, tzinfo=UTC)
        now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        trigger = MagicMock()
        trigger.get_next_fire_time.side_effect = [first, RuntimeError("bad")]

        assert dur.previous_fire_time(trigger, now, now - timedelta(hours=6)) == first

    def test_a_trigger_that_ends_keeps_what_it_found(self):
        first = datetime(2026, 8, 6, 8, 0, tzinfo=UTC)
        now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        trigger = MagicMock()
        trigger.get_next_fire_time.side_effect = [first, None]

        assert dur.previous_fire_time(trigger, now, now - timedelta(hours=6)) == first


class TestCatchUpDispatch:
    @pytest.mark.asyncio
    async def test_a_missed_job_with_no_callable_is_skipped(self):
        """Defensive: the id/trigger list and the func map can disagree."""
        now = datetime(2026, 8, 6, 8, 5, tzinfo=UTC)
        jobs = [("morning_checkin", _cron(hour=8, minute=0), None)]

        with patch.object(dur, "load_last_fired", AsyncMock(return_value={})):
            ran = await dur.run_catch_up(jobs, now=now)

        assert ran == []
