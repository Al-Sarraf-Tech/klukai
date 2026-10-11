"""The schedule — which jobs the ProactiveEngine runs, and when.

``ScheduleMixin`` holds the APScheduler job registration table (every job id
and its cron expression, pinned to the Commander's wall clock), the thin
scheduled-job adapters that only delegate to another app module (command
decisions, portrait backfill, the deferred-task sweep), and the startup replay
of jobs whose fire time passed while the process was down.

Scheduler construction, the error/run listeners and the ``start()``/``stop()``
lifecycle stay on ``ProactiveEngine`` in ``engine.py``; ``start()`` calls
``_register_jobs()`` first, then wires the listeners and starts the scheduler.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from apscheduler.triggers.cron import CronTrigger

from .base import _EngineBase
from .state import LOCAL_TZ, QUIET_HOUR_END

if TYPE_CHECKING:
    from .engine import ProactiveEngine

logger = logging.getLogger(__name__)


def _cron(**fields) -> CronTrigger:
    """A CronTrigger pinned to the Commander's timezone (America/Chicago).

    CronTrigger resolves its timezone at construction, so every trigger must
    carry it explicitly — hours below are local wall-clock, never UTC.
    """
    return CronTrigger(timezone=LOCAL_TZ, **fields)


class ScheduleMixin(_EngineBase):
    """Job registration table + scheduled-job adapters for ProactiveEngine."""

    def _register_jobs(self) -> None:
        """Register every scheduled job on ``self._scheduler``.

        Called first thing from ``ProactiveEngine.start()``, before the
        error/run listeners are wired and the scheduler is started. Hours are
        local wall-clock (``_cron`` pins America/Chicago), never UTC.
        """
        # Morning briefing at 0800
        self._scheduler.add_job(
            self._morning_checkin,
            _cron(hour=8, minute=0),
            id="morning_checkin",
            replace_existing=True,
        )

        # Daily challenge at 09:00
        self._scheduler.add_job(
            self._daily_challenge,
            _cron(hour=9, minute=0),
            id="daily_challenge",
            replace_existing=True,
        )

        # Promise follow-ups — check thrice daily (10:00 / 14:00 / 18:00 local)
        self._scheduler.add_job(
            self._promise_followup_check,
            _cron(hour="10,14,18", minute=0),
            id="promise_followup",
            replace_existing=True,
        )

        # Evening wind-down at 2200
        self._scheduler.add_job(
            self._evening_checkin,
            _cron(hour=22, minute=0),
            id="evening_checkin",
            replace_existing=True,
        )

        # Idle check every 2 hours during operational hours
        self._scheduler.add_job(
            self._idle_check,
            _cron(hour="9-21/2", minute=30),
            id="idle_check",
            replace_existing=True,
        )

        # Unsent messages — fires on same schedule as idle, separate roll
        self._scheduler.add_job(
            self._unsent_message_check,
            _cron(hour="10-22/3", minute=15),
            id="unsent_message_check",
            replace_existing=True,
        )

        # Mission report — once per day, random afternoon
        self._scheduler.add_job(
            self._mission_report,
            _cron(hour=14, minute=45),
            id="mission_report",
            replace_existing=True,
        )

        # Random lore events — every 30 min during normal hours. The last slot
        # is 22:45: QUIET_HOUR_START is 2300, and _random_event does not go
        # through _can_send(), so the schedule itself must stop short of it.
        self._scheduler.add_job(
            self._random_event,
            _cron(hour="9-22", minute="15,45"),
            id="random_event",
            replace_existing=True,
        )

        # Mission events — every 5 min, 24/7 (only fires if mission is active)
        self._scheduler.add_job(
            self._mission_random_event,
            "interval",
            minutes=5,
            id="mission_random_event",
            replace_existing=True,
        )

        # Late-night dreams — fires once between 1-4 AM if conditions are right
        self._scheduler.add_job(
            self._dream_event,
            _cron(hour="1-4", minute=37),
            id="dream_event",
            replace_existing=True,
        )

        # Living memory recall — warm "remember when…" from the real archive.
        # Fires at two daytime hours; each tick rolls ~35% so it isn't every day.
        self._scheduler.add_job(
            self._memory_recall_tick,
            _cron(hour="11,17", minute=20),
            id="memory_recall",
            replace_existing=True,
        )

        # Spontaneous art — she occasionally draws something for the Commander
        # unprompted and leaves it in the album. ~18% roll per fire + a ~2.5-day
        # cooldown keep it rare and treasured.
        self._scheduler.add_job(
            self._spontaneous_art_tick,
            _cron(hour="15,19", minute=40),
            id="spontaneous_art",
            replace_existing=True,
        )

        # Daily recap at 2100
        self._scheduler.add_job(
            self._daily_recap,
            _cron(hour=21, minute=0),
            id="daily_recap",
            replace_existing=True,
        )

        # Evening romance window at 20:30 local
        self._scheduler.add_job(
            self._romance_window,
            _cron(hour=20, minute=30),
            id="romance_window",
            replace_existing=True,
        )

        # Reset daily counter at midnight
        self._scheduler.add_job(
            self._reset_daily,
            _cron(hour=0, minute=0),
            id="daily_reset",
            replace_existing=True,
        )

        # Weekly reflection: Sunday 21:00 local
        self._scheduler.add_job(
            self._weekly_reflection,
            _cron(day_of_week="sun", hour=21, minute=0),
            id="weekly_reflection",
            replace_existing=True,
        )

        # Daily anniversary check: 08:00 local, the first minute outside quiet
        # hours. It used to run at 07:58, "just before the morning greeting",
        # but _anniversary_check now goes through _can_send(), which refuses
        # anything before QUIET_HOUR_END — so it shares the greeting's minute.
        self._scheduler.add_job(
            self._anniversary_check,
            _cron(hour=QUIET_HOUR_END, minute=0),
            id="anniversary_check",
            replace_existing=True,
        )

        # Seasonal/holiday greeting check: daily at 09:00. Matches today's
        # month/day against seasonal_events; fires once per occurrence.
        self._scheduler.add_job(
            self._seasonal_check,
            _cron(hour=9, minute=0),
            id="seasonal_check",
            replace_existing=True,
        )

        # Pattern-aware "quiet day" check-in: early afternoon, after the day's
        # activity (or lack of it) is established. Once/day, gated on a strong
        # low-activity weekday pattern matching today.
        self._scheduler.add_job(
            self._quiet_day_check,
            _cron(hour=15, minute=10),
            id="quiet_day_check",
            replace_existing=True,
        )

        # Command decisions: she may bring him a small call from her life
        # (app/decisions.py). Rolled per slot; capped at 2 per rolling week.
        self._scheduler.add_job(
            self._command_decision_check,
            _cron(hour=14, minute=40),
            id="command_decision",
            replace_existing=True,
        )

        # Live Portrait idle backfill (opt-in, PORTRAIT_BACKFILL=1): draw the
        # other unlocked outfits one set at a time in the small hours.
        self._scheduler.add_job(
            self._portrait_backfill,
            _cron(hour="2-6", minute="*/15"),
            id="portrait_backfill",
            replace_existing=True,
        )

        # Deferred-task safety net: fire anything already due that the RabbitMQ
        # delay rail did not deliver. This is what turns a broker outage into
        # late delivery instead of lost work, so it must stay unconditional.
        self._scheduler.add_job(
            self._deferred_sweep,
            "interval",
            minutes=1,
            id="deferred_sweep",
            replace_existing=True,
        )

    async def _command_decision_check(self) -> None:
        """Maybe ask the Commander to make a call (see app/decisions.py)."""
        from ..decisions import PRIMARY_USER, maybe_ask
        # maybe_ask is typed against the full engine; this mixin is only ever
        # composed into ProactiveEngine, so the cast is type-only (no runtime
        # effect) and names that invariant for the checker.
        await maybe_ask(cast("ProactiveEngine", self), PRIMARY_USER)

    async def _portrait_backfill(self) -> None:
        """Opt-in idle backfill of portrait sets (see app/portrait.py)."""
        from ..decisions import PRIMARY_USER
        from ..portrait import backfill_tick
        await backfill_tick(PRIMARY_USER)

    async def _deferred_sweep(self) -> None:
        """Backstop for the RabbitMQ delay rail."""
        from ..deferred import sweep
        await sweep()

    async def _catch_up_missed(self) -> None:
        """Run allowlisted jobs whose fire time passed while she was down."""
        from .durability import run_catch_up

        jobs = [
            (job.id, job.trigger, job.func)
            for job in self._scheduler.get_jobs()
        ]
        ran = await run_catch_up(jobs)
        if ran:
            logger.info("Scheduler catch-up replayed: %s", ", ".join(ran))
