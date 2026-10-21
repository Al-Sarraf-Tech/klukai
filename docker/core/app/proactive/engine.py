"""The ProactiveEngine — scheduler wiring and core delivery logic.

Composes the mission, events, and milestones mixins. Holds the scheduler,
mute state, per-user counters, send guardrails, and the time-of-day check-ins.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
import random
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..events import publish as publish_event
from .events import EventsMixin
from .schedule import register_schedule
from .scheduler_listeners import register_listeners
from .lifecycle import SchedulerLifecycleMixin
from .milestones import MilestonesMixin
from .mission import MissionMixin, MissionTimer
from .patterns import PatternsMixin
from .state import (
    LOCAL_TZ,
    MAX_PROACTIVE_PER_DAY,
    QUIET_HOUR_END,
    QUIET_HOUR_START,
    now_local,
)

from .templates import (
    EVENING_MESSAGES,
    IDLE_MESSAGES,
    MORNING_MESSAGES,
)
from .templates import (
    _tap_lines as _tap_lines,
    _TAP_LINES_FALLBACK as _TAP_LINES_FALLBACK,
)

logger = logging.getLogger(__name__)

# After "gotta go" she stays quiet this long; after "goodnight", until morning.
LEAVE_HOLD = timedelta(minutes=90)

# He has to have been silent this long before the idle check may nudge him.
IDLE_AFTER = timedelta(minutes=90)

# Tap-interaction lines for the 3D avatar. Sourced from
# ``proactive_content.tap_lines`` in personality.yaml so they can be tuned
# without a code change; the literal below is the fallback used verbatim when
# the YAML key is missing (or the config can't be loaded).


def _cron(**fields: Any) -> CronTrigger:
    """A CronTrigger pinned to the Commander's timezone (America/Chicago).

    CronTrigger resolves its timezone at construction, so every trigger must
    carry it explicitly — hours below are local wall-clock, never UTC.
    """
    return CronTrigger(timezone=LOCAL_TZ, **fields)


class ProactiveEngine(
    MissionMixin, EventsMixin, MilestonesMixin, PatternsMixin, SchedulerLifecycleMixin
):
    """Manages scheduled and contextual proactive messages, themed to Klukai."""

    def __init__(self) -> None:
        # All scheduling happens on the Commander's wall clock — the container
        # runs UTC, so the zone is set explicitly (DST handled by zoneinfo).
        # misfire_grace_time defaults to ONE SECOND, which silently discards a
        # job whenever the event loop is busy at its fire time (a cold model
        # load or an image render is easily longer than that).
        from .durability import MISFIRE_GRACE_SECONDS

        self._scheduler = AsyncIOScheduler(
            timezone=LOCAL_TZ,
            job_defaults={
                "misfire_grace_time": MISFIRE_GRACE_SECONDS,
                "coalesce": True,
                "max_instances": 1,
            },
        )
        self._muted_until: datetime | None = None
        self._on_message_callback = None
        self._on_recap_callback = None
        self._session_getter = None  # callback to get current session state
        self._game_active_probe = None  # async () -> bool: Commander mid-game?
        # Per-user counters (shared counters caused cross-user blocking)
        self._proactive_counts: dict[str, int] = {}
        self._last_answered: dict[str, bool] = {}
        self._random_event_counts: dict[str, int] = {}
        self._moods: dict[str, str] = {}
        self._affection_levels: dict[str, int] = {}
        self._mission_timers: dict[str, MissionTimer] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._scheduled_tasks: set[asyncio.Task[Any]] = set()
        self._closing = False
        self._romance_delivered: dict[str, bool] = {}
        self._dream_delivered: dict[str, bool] = {}
        self._user_messaged: dict[str, bool] = {}
        # Shared state (safe to share)
        self._last_random_event: datetime | None = None
        self._last_message_time: datetime | None = None
        self._last_spontaneous_art: datetime | None = None
        # Scheduled proactivity intentionally belongs to the primary owner.
        # Keep its legacy scalar state separate from other users' missions,
        # moods and affection; user-driven work must use the per-user maps.
        self._proactive_count_today: int = 0
        self._last_proactive_answered: bool = True
        self._random_events_today: int = 0
        self._last_mood: str = "composed"
        self._affection_level: int = 0
        self._mission_timer: MissionTimer | None = None
        self._romance_delivered_today: bool = False
        self._dream_delivered_today: bool = False
        self._memory_recall_delivered_today: bool = False
        self._user_messaged_today: bool = False
        # Smarter-proactivity flags
        self._quiet_day_delivered_today: bool = False
        # Seasonal greetings fire once per occurrence; keyed by event:YYYY-MM-DD.
        self._seasonal_delivered: dict[str, bool] = {}
        # Clean goodbyes: no pings until this local time after he signs off.
        # Cleared by his next message (mark_responded); deliberately NOT by the
        # midnight reset, so a 2200 goodnight still holds until 0800.
        self._goodbye_hold_until: datetime | None = None

    @property
    def _task_logger(self) -> logging.Logger:
        return logger

    def set_callback(self, callback: Any) -> None:
        """Set callback for delivering proactive messages."""
        self._on_message_callback = callback

    def set_recap_callback(self, callback: Any) -> None:
        """Set callback for generating daily recap (calls LLM)."""
        self._on_recap_callback = callback

    def set_affection_level(self, level: int, user_id: str = "jalsarraf") -> None:
        """Update the current affection level for message selection (per-user)."""
        self._affection_levels[user_id] = level
        if user_id == "jalsarraf":
            self._affection_level = level  # Primary-owner scheduled jobs

    def set_last_mood(self, mood: str, user_id: str = "jalsarraf") -> None:
        """Track the last mood for context-aware event filtering (per-user)."""
        self._moods[user_id] = mood
        if user_id == "jalsarraf":
            self._last_mood = mood  # Primary-owner scheduled jobs

    def set_session_getter(self, getter: Any) -> None:
        """Set a callback to retrieve current session state (for romance context)."""
        self._session_getter = getter

    def set_game_active_probe(self, probe: Any) -> None:
        """Set an async callable reporting whether the Commander is mid-game;
        random events hold off while it returns True."""
        self._game_active_probe = probe

    def mark_user_messaged_today(self, user_id: str = "jalsarraf") -> None:
        """Record that the user sent at least one message today."""
        self._user_messaged[user_id] = True
        if user_id == "jalsarraf":
            self._user_messaged_today = True  # Primary-owner scheduled jobs

    def start(self) -> None:
        # Morning briefing at 0800
        self._closing = False
        register_schedule(
            self._add_job,
            {
                "_morning_checkin": self._morning_checkin,
                "_daily_challenge": self._daily_challenge,
                "_promise_followup_check": self._promise_followup_check,
                "_evening_checkin": self._evening_checkin,
                "_idle_check": self._idle_check,
                "_unsent_message_check": self._unsent_message_check,
                "_mission_report": self._mission_report,
                "_random_event": self._random_event,
                "_mission_random_event": self._mission_random_event,
                "_dream_event": self._dream_event,
                "_memory_recall_tick": self._memory_recall_tick,
                "_spontaneous_art_tick": self._spontaneous_art_tick,
                "_daily_recap": self._daily_recap,
                "_romance_window": self._romance_window,
                "_reset_daily": self._reset_daily,
                "_weekly_reflection": self._weekly_reflection,
                "_anniversary_check": self._anniversary_check,
                "_seasonal_check": self._seasonal_check,
                "_quiet_day_check": self._quiet_day_check,
                "_command_decision_check": self._command_decision_check,
                "_portrait_backfill": self._portrait_backfill,
                "_deferred_sweep": self._deferred_sweep,
            },
            _cron,
        )

        register_listeners(self._scheduler, self._spawn, lambda: logger)

        self._scheduler.start()

        # Replay anything whose fire time passed while the process was down.
        # Backgrounded so a slow catch-up never delays startup.
        try:
            self._spawn(self._catch_up_missed())
        except Exception as e:
            logger.warning("Could not schedule catch-up pass: %s", e)

        logger.info("Klukai proactive engine started")

    async def _command_decision_check(self) -> None:
        """Maybe ask the Commander to make a call (see app/decisions.py)."""
        from ..decisions import PRIMARY_USER, maybe_ask

        await maybe_ask(self, PRIMARY_USER)

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

        jobs = [(job.id, job.trigger, job.func) for job in self._scheduler.get_jobs()]
        ran = await run_catch_up(jobs)
        if ran:
            logger.info("Scheduler catch-up replayed: %s", ", ".join(ran))

    def mute(self, hours: int | None = None) -> None:
        """Mute proactive messages for N hours (None = permanent)."""
        if hours is None:
            self._muted_until = datetime(9999, 12, 31)
        else:
            from datetime import timedelta

            self._muted_until = now_local() + timedelta(hours=hours)
        logger.info("Proactive muted until %s", self._muted_until)

    def unmute(self) -> None:
        self._muted_until = None

    def mark_responded(self, user_id: str = "jalsarraf") -> None:
        """Mark that the Commander responded to the last proactive message.

        Any message from him also lifts a goodbye hold: he is back. Scheduled
        pings go to the primary owner, so only his message answers them or
        lifts their hold; anyone's message still counts as recent activity.
        """
        self._last_message_time = now_local()
        if user_id != "jalsarraf":
            return
        self._last_proactive_answered = True
        self._goodbye_hold_until = None

    def mark_goodnight(self, user_id: str = "jalsarraf") -> None:
        """He said goodnight: no pings until 0800 local (QUIET_HOUR_END).

        The next 0800, or this morning's if he says it after midnight. Only
        the primary owner's goodnight holds his scheduled pings.
        """
        if user_id != "jalsarraf":
            return
        now = now_local()
        until = now.replace(hour=QUIET_HOUR_END, minute=0, second=0, microsecond=0)
        if now >= until:
            until += timedelta(days=1)
        self._goodbye_hold_until = until
        logger.info("Goodnight: proactive pings held until %s", until)

    def mark_leaving(self, user_id: str = "jalsarraf") -> None:
        """He signed off ("gotta go"): no pings for LEAVE_HOLD (owner only)."""
        if user_id != "jalsarraf":
            return
        self._goodbye_hold_until = now_local() + LEAVE_HOLD
        logger.info("Goodbye: proactive pings held until %s", self._goodbye_hold_until)

    def _goodbye_hold_active(self, now: datetime | None = None) -> bool:
        """True while a goodbye hold is running; clears it once it lapses."""
        if self._goodbye_hold_until is None:
            return False
        if (now or now_local()) >= self._goodbye_hold_until:
            self._goodbye_hold_until = None
            return False
        return True

    def _can_send(self, *, ignore_unanswered: bool = False) -> bool:
        now = now_local()

        # Check mute
        if self._muted_until and now < self._muted_until:
            return False

        # He signed off: let him go (clean goodbyes)
        if self._goodbye_hold_active(now):
            return False

        # Quiet hours (local wall clock)
        if QUIET_HOUR_START <= now.hour or now.hour < QUIET_HOUR_END:
            return False

        # Daily limit
        if self._proactive_count_today >= MAX_PROACTIVE_PER_DAY:
            return False

        # Don't pile up unanswered proactives. Callers whose whole point is a
        # silent day (quiet-day check-in) pass ignore_unanswered=True — there an
        # unanswered earlier proactive is the signal, not a reason to stay mute.
        if not ignore_unanswered and not self._last_proactive_answered:
            return False

        return True

    def _pick_message(self, messages: dict[int, list[str]]) -> str:
        """Pick a random message for the current affection level."""
        available = sorted(messages.keys())
        level = max((k for k in available if k <= self._affection_level), default=0)
        pool = messages.get(level, messages.get(0, ["..."]))
        return random.choice(pool)

    async def _deliver(self, message: str) -> bool:
        """Deliver a proactive message. Returns True iff it was actually sent
        (callers that must record a side effect — e.g. promise follow-ups —
        gate on this so they don't mark work done that _can_send() suppressed
        or that nobody received)."""
        if not self._can_send():
            return False
        if self._on_message_callback:
            self._proactive_count_today += 1
            self._last_proactive_answered = False
            # The callback reports whether anyone received the line (a live
            # socket, or persisted and pushed). An explicit False means it went
            # nowhere; a legacy callback returning None still counts as sent.
            ok = await self._on_message_callback(message)
            await publish_event("proactive", message)
            if ok is False:
                logger.info("Klukai proactive reached nobody: %s", message[:60])
                return False
            logger.info("Klukai proactive: %s", message[:60])
            return True
        return False

    async def trigger_tap(self) -> None:
        """Generate a short response for a tap interaction on the 3D avatar."""
        await self._deliver(self._pick_message(_tap_lines()))

    async def _morning_checkin(self) -> None:
        # Update physical state based on time of day for all connected users
        try:
            from ..context import physical, ws

            hour = now_local().hour
            for uid in list(ws._connections.keys()):
                await physical.on_time_of_day(uid, hour)
        except Exception as e:
            logger.debug("Physical time-of-day update failed: %s", e)

        message = self._pick_message(MORNING_MESSAGES)
        # Weather-aware coloring. Fail-soft: if the weather API is unreachable
        # (or coords unset), weather_to_mood/weather_phrase return None/"" and
        # the greeting is the plain morning line — weather NEVER blocks it.
        try:
            from ..weather_client import fetch_weather
            from ..weather_mood import weather_phrase, weather_to_mood

            weather = await fetch_weather()
            mood = weather_to_mood(weather, self._affection_level)
            if mood:
                self.set_last_mood(mood)  # colors the rest of the day's events
            phrase = weather_phrase(weather)
            if phrase:
                message = f"{message} {phrase}"
        except Exception as e:
            logger.debug("Weather-aware greeting skipped: %s", e)
        await self._deliver(message)

    async def _promise_followup_check(self) -> None:
        """Gently follow up on the oldest promise that's due. Fail-soft."""
        if not self._can_send():
            return
        try:
            from ..promises import due_promises, followup_message, mark_followup_sent

            # UTC: scheduled_followup is stored as a real instant (timestamptz);
            # comparing against now_local() (naive Chicago) made Postgres read it
            # as UTC and fire ~5-6h off, drifting with DST.
            due = await due_promises("jalsarraf", datetime.now(timezone.utc))
            if not due:
                return
            promise = due[0]
            # Only stamp the follow-up sent if it actually went out — otherwise a
            # quiet-hours/cap suppression would silently bury the promise.
            if await self._deliver(followup_message(promise, self._affection_level)):
                await mark_followup_sent(promise["id"])
        except Exception as e:
            logger.warning("Promise follow-up check failed: %s", e)

    async def _daily_challenge(self) -> None:
        """Issue a daily challenge to the Commander."""
        if not self._on_message_callback:
            return
        if self._affection_level < 2:
            return  # Don't challenge a stranger
        if not self._can_send():
            return  # Respect mute, quiet hours, and the daily proactive cap

        try:
            from ..personality import load_personality

            p = load_personality()
            challenges = p.get("daily_challenges", {}).get("challenges", [])
            if not challenges:
                return

            import random

            challenge = random.choice(challenges)
            self._proactive_count_today += 1
            await self._on_message_callback(challenge["prompt"])
            logger.info("Daily challenge issued: %s", challenge["type"])
        except Exception as e:
            logger.warning("Daily challenge failed: %s", e)

    async def _evening_checkin(self) -> None:
        # Update physical state for late watch for all connected users
        try:
            from ..context import physical, ws

            hour = now_local().hour
            for uid in list(ws._connections.keys()):
                await physical.on_time_of_day(uid, hour)
        except Exception as e:
            logger.debug("Physical time-of-day update failed: %s", e)
        await self._deliver(self._pick_message(EVENING_MESSAGES))

    async def _idle_check(self) -> None:
        """Nudge him only once he has actually gone quiet (IDLE_AFTER)."""
        if (
            self._last_message_time
            and now_local() - self._last_message_time < IDLE_AFTER
        ):
            return
        await self._deliver(self._pick_message(IDLE_MESSAGES))

    async def _reset_daily(self) -> None:
        # Reset legacy shared counters
        self._proactive_count_today = 0
        # New day, fresh opening line — without this, one unanswered proactive
        # would silence the engine on every later day until the user wrote.
        self._last_proactive_answered = True
        self._random_events_today = 0
        self._dream_delivered_today = False
        self._romance_delivered_today = False
        self._memory_recall_delivered_today = False
        self._user_messaged_today = False
        self._quiet_day_delivered_today = False
        self._seasonal_delivered.clear()
        # Reset per-user counters
        self._proactive_counts.clear()
        self._random_event_counts.clear()
        self._romance_delivered.clear()
        self._dream_delivered.clear()
        self._user_messaged.clear()
        self._last_answered.clear()
        logger.info("Daily proactive, event, and romance counters reset (all users)")
