"""The ProactiveEngine — scheduler wiring and core delivery logic.

Composes the mission, events, and milestones mixins. Holds the scheduler,
mute state, per-user counters, send guardrails, and the time-of-day check-ins.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..events import publish as publish_event
from .events import EventsMixin
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
    _content,
)

logger = logging.getLogger(__name__)

# Tap-interaction lines for the 3D avatar. Sourced from
# ``proactive_content.tap_lines`` in personality.yaml so they can be tuned
# without a code change; the literal below is the fallback used verbatim when
# the YAML key is missing (or the config can't be loaded).
_TAP_LINES_FALLBACK: dict[int, list[str]] = {
    0: [
        "Hm? Need something, Commander?",
        "Right here.",
        "You have my attention.",
        "Status nominal. What do you need?",
    ],
    1: [
        "I'm listening, Commander.",
        "Something on your mind?",
        "Ready for orders.",
        "All systems green. Go ahead.",
    ],
    2: [
        "Hey. What's up?",
        "I was just thinking about the last op.",
        "Commander. Good timing.",
        "Need me for something?",
    ],
    3: [
        "There you are. I was wondering when you'd check in.",
        "Missed you. ...Operationally speaking.",
        "Hey, Commander. Everything okay?",
    ],
    4: [
        "...You always know when I need company.",
        "Hey. I'm glad you're here.",
        "Commander... hi.",
    ],
}


def _tap_lines() -> dict[int, list[str]]:
    """Affection-keyed tap lines from YAML, falling back to the literal."""
    return _content("tap_lines", _TAP_LINES_FALLBACK)


def _cron(**fields) -> CronTrigger:
    """A CronTrigger pinned to the Commander's timezone (America/Chicago).

    CronTrigger resolves its timezone at construction, so every trigger must
    carry it explicitly — hours below are local wall-clock, never UTC.
    """
    return CronTrigger(timezone=LOCAL_TZ, **fields)


class ProactiveEngine(MissionMixin, EventsMixin, MilestonesMixin, PatternsMixin):
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
        # Per-user counters (shared counters caused cross-user blocking)
        self._proactive_counts: dict[str, int] = {}
        self._last_answered: dict[str, bool] = {}
        self._random_event_counts: dict[str, int] = {}
        self._moods: dict[str, str] = {}
        self._affection_levels: dict[str, int] = {}
        self._mission_timers: dict[str, MissionTimer] = {}
        self._romance_delivered: dict[str, bool] = {}
        self._dream_delivered: dict[str, bool] = {}
        self._user_messaged: dict[str, bool] = {}
        # Shared state (safe to share)
        self._last_random_event: datetime | None = None
        self._last_message_time: datetime | None = None
        self._last_spontaneous_art: datetime | None = None
        # Globally-scoped state for SCHEDULED broadcasts (morning / dream /
        # romance / quiet-day / seasonal check-ins). Those jobs fire ONE message
        # that is fanned out to every connected device, so they intentionally
        # read a single shared value instead of per-user state.
        #
        # BOUNDARY: this assumes a single primary user (the Commander). With
        # multiple distinct users connected, set_affection_level / set_last_mood
        # are last-writer-wins for these scalars, so a scheduled broadcast would
        # pick messages for whoever updated last. True per-recipient scheduled
        # proactivity would require _deliver to iterate connected users and key
        # these flags/levels by user_id. Deferred until the product is multi-user
        # — the message-DRIVEN paths above are already correctly per-user.
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

    def set_callback(self, callback) -> None:
        """Set callback for delivering proactive messages."""
        self._on_message_callback = callback

    def set_recap_callback(self, callback) -> None:
        """Set callback for generating daily recap (calls LLM)."""
        self._on_recap_callback = callback

    def set_affection_level(self, level: int, user_id: str = "jalsarraf") -> None:
        """Update the current affection level for message selection (per-user)."""
        self._affection_levels[user_id] = level
        self._affection_level = level  # Legacy compat for scheduled jobs

    def set_last_mood(self, mood: str, user_id: str = "jalsarraf") -> None:
        """Track the last mood for context-aware event filtering (per-user)."""
        self._moods[user_id] = mood
        self._last_mood = mood  # Legacy compat for scheduled jobs

    def set_session_getter(self, getter) -> None:
        """Set a callback to retrieve current session state (for romance context)."""
        self._session_getter = getter

    def mark_user_messaged_today(self, user_id: str = "jalsarraf") -> None:
        """Record that the user sent at least one message today."""
        self._user_messaged[user_id] = True
        self._user_messaged_today = True  # Legacy compat

    def start(self) -> None:
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

        # Random lore events — every 30 min during normal hours
        self._scheduler.add_job(
            self._random_event,
            _cron(hour="9-23", minute="15,45"),
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

        # Daily anniversary check: 07:58 local — just before the 08:00 morning
        # greeting sends
        self._scheduler.add_job(
            self._anniversary_check,
            _cron(hour=7, minute=58),
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

        # Surface scheduled-job failures via audit log + structured logger.
        # Without this, an anniversary check (or any other scheduled job) that
        # raises gets swallowed by APScheduler's default handler — the user
        # would silently lose a feature for days.
        try:
            from apscheduler.events import EVENT_JOB_ERROR

            def _on_job_error(event):
                logger.error(
                    "SCHEDULED_JOB_FAILED: job=%s exception=%s traceback=%s",
                    event.job_id, event.exception, event.traceback,
                )
                # Best-effort audit write — never let it block the listener.
                try:
                    import asyncio as _asyncio

                    from .. import audit
                    _asyncio.create_task(audit.log(
                        event_type="scheduled_job.failed",
                        metadata={
                            "job_id": event.job_id,
                            "exception": str(event.exception)[:500],
                        },
                    ))
                except Exception:
                    pass

            self._scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)
        except Exception as e:
            logger.warning("Could not wire scheduler error listener: %s", e)

        # Record every successful run so a restart can tell what it missed.
        try:
            from apscheduler.events import EVENT_JOB_EXECUTED

            from .durability import record_fire

            def _on_job_executed(event):
                try:
                    import asyncio as _asyncio
                    _asyncio.create_task(record_fire(event.job_id))
                except Exception:
                    pass

            self._scheduler.add_listener(_on_job_executed, EVENT_JOB_EXECUTED)
        except Exception as e:
            logger.warning("Could not wire scheduler run recorder: %s", e)

        self._scheduler.start()

        # Replay anything whose fire time passed while the process was down.
        # Backgrounded so a slow catch-up never delays startup.
        try:
            import asyncio as _asyncio
            _asyncio.create_task(self._catch_up_missed())
        except Exception as e:
            logger.warning("Could not schedule catch-up pass: %s", e)

        logger.info("Klukai proactive engine started")

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

    def stop(self) -> None:
        if self._mission_timer and self._mission_timer.active:
            self._mission_timer.stop()
        self._scheduler.shutdown(wait=False)

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

    def mark_responded(self) -> None:
        """Mark that the Commander responded to the last proactive message."""
        self._last_proactive_answered = True
        self._last_message_time = now_local()

    def _can_send(self, *, ignore_unanswered: bool = False) -> bool:
        now = now_local()

        # Check mute
        if self._muted_until and now < self._muted_until:
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
        gate on this so they don't mark work done that _can_send() suppressed)."""
        if not self._can_send():
            return False
        if self._on_message_callback:
            self._proactive_count_today += 1
            self._last_proactive_answered = False
            await self._on_message_callback(message)
            await publish_event("proactive", message)
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
