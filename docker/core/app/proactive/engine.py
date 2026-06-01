"""The ProactiveEngine — scheduler wiring and core delivery logic.

Composes the mission, events, and milestones mixins. Holds the scheduler,
mute state, per-user counters, send guardrails, and the time-of-day check-ins.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..events import publish as publish_event
from .events import EventsMixin
from .milestones import MilestonesMixin
from .mission import MissionMixin, MissionTimer
from .patterns import PatternsMixin
from .state import (
    MAX_PROACTIVE_PER_DAY,
    QUIET_HOUR_END,
    QUIET_HOUR_START,
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


class ProactiveEngine(MissionMixin, EventsMixin, MilestonesMixin, PatternsMixin):
    """Manages scheduled and contextual proactive messages, themed to Klukai."""

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()
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
            CronTrigger(hour=8, minute=0),
            id="morning_checkin",
            replace_existing=True,
        )

        # Daily challenge at 09:00
        self._scheduler.add_job(
            self._daily_challenge,
            CronTrigger(hour=9, minute=0),
            id="daily_challenge",
            replace_existing=True,
        )

        # Evening wind-down at 2200
        self._scheduler.add_job(
            self._evening_checkin,
            CronTrigger(hour=22, minute=0),
            id="evening_checkin",
            replace_existing=True,
        )

        # Idle check every 2 hours during operational hours
        self._scheduler.add_job(
            self._idle_check,
            CronTrigger(hour="9-21/2", minute=30),
            id="idle_check",
            replace_existing=True,
        )

        # Unsent messages — fires on same schedule as idle, separate roll
        self._scheduler.add_job(
            self._unsent_message_check,
            CronTrigger(hour="10-22/3", minute=15),
            id="unsent_message_check",
            replace_existing=True,
        )

        # Mission report — once per day, random afternoon
        self._scheduler.add_job(
            self._mission_report,
            CronTrigger(hour=14, minute=45),
            id="mission_report",
            replace_existing=True,
        )

        # Random lore events — every 30 min during normal hours
        self._scheduler.add_job(
            self._random_event,
            CronTrigger(hour="9-23", minute="15,45"),
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
            CronTrigger(hour="1-4", minute=37),
            id="dream_event",
            replace_existing=True,
        )

        # Living memory recall — warm "remember when…" from the real archive.
        # Fires at two daytime hours; each tick rolls ~35% so it isn't every day.
        self._scheduler.add_job(
            self._memory_recall_tick,
            CronTrigger(hour="11,17", minute=20),
            id="memory_recall",
            replace_existing=True,
        )

        # Spontaneous art — she occasionally draws something for the Commander
        # unprompted and leaves it in the album. ~18% roll per fire + a ~2.5-day
        # cooldown keep it rare and treasured.
        self._scheduler.add_job(
            self._spontaneous_art_tick,
            CronTrigger(hour="15,19", minute=40),
            id="spontaneous_art",
            replace_existing=True,
        )

        # Daily recap at 2100
        self._scheduler.add_job(
            self._daily_recap,
            CronTrigger(hour=21, minute=0),
            id="daily_recap",
            replace_existing=True,
        )

        # Evening romance window at 20:30 CST (01:30 UTC next day)
        # CST = UTC-6, so 20:30 CST = 02:30 UTC
        self._scheduler.add_job(
            self._romance_window,
            CronTrigger(hour=2, minute=30),  # 20:30 CST in UTC
            id="romance_window",
            replace_existing=True,
        )

        # Reset daily counter at midnight
        self._scheduler.add_job(
            self._reset_daily,
            CronTrigger(hour=0, minute=0),
            id="daily_reset",
            replace_existing=True,
        )

        # Weekly reflection: Sunday 21:00 CST (03:00 UTC Monday)
        self._scheduler.add_job(
            self._weekly_reflection,
            CronTrigger(day_of_week="mon", hour=3, minute=0),
            id="weekly_reflection",
            replace_existing=True,
        )

        # Daily anniversary check: 14:00 UTC (~08:00 CST) — before morning greeting sends
        self._scheduler.add_job(
            self._anniversary_check,
            CronTrigger(hour=13, minute=58),
            id="anniversary_check",
            replace_existing=True,
        )

        # Seasonal/holiday greeting check: daily at 09:00. Matches today's
        # month/day against seasonal_events; fires once per occurrence.
        self._scheduler.add_job(
            self._seasonal_check,
            CronTrigger(hour=9, minute=0),
            id="seasonal_check",
            replace_existing=True,
        )

        # Pattern-aware "quiet day" check-in: early afternoon, after the day's
        # activity (or lack of it) is established. Once/day, gated on a strong
        # low-activity weekday pattern matching today.
        self._scheduler.add_job(
            self._quiet_day_check,
            CronTrigger(hour=15, minute=10),
            id="quiet_day_check",
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

        self._scheduler.start()
        logger.info("Klukai proactive engine started")

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
            self._muted_until = datetime.now() + timedelta(hours=hours)
        logger.info("Proactive muted until %s", self._muted_until)

    def unmute(self) -> None:
        self._muted_until = None

    def mark_responded(self) -> None:
        """Mark that the Commander responded to the last proactive message."""
        self._last_proactive_answered = True
        self._last_message_time = datetime.now()

    def _can_send(self) -> bool:
        now = datetime.now()

        # Check mute
        if self._muted_until and now < self._muted_until:
            return False

        # Quiet hours
        if QUIET_HOUR_START <= now.hour or now.hour < QUIET_HOUR_END:
            return False

        # Daily limit
        if self._proactive_count_today >= MAX_PROACTIVE_PER_DAY:
            return False

        # Don't pile up unanswered proactives
        if not self._last_proactive_answered:
            return False

        return True

    def _pick_message(self, messages: dict[int, list[str]]) -> str:
        """Pick a random message for the current affection level."""
        available = sorted(messages.keys())
        level = max((k for k in available if k <= self._affection_level), default=0)
        pool = messages.get(level, messages.get(0, ["..."]))
        return random.choice(pool)

    async def _deliver(self, message: str) -> None:
        if not self._can_send():
            return
        if self._on_message_callback:
            self._proactive_count_today += 1
            self._last_proactive_answered = False
            await self._on_message_callback(message)
            await publish_event("proactive", message)
            logger.info("Klukai proactive: %s", message[:60])

    async def trigger_tap(self) -> None:
        """Generate a short response for a tap interaction on the 3D avatar."""
        await self._deliver(self._pick_message(_tap_lines()))

    async def _morning_checkin(self) -> None:
        # Update physical state based on time of day for all connected users
        try:
            from ..context import physical, ws
            hour = datetime.now().hour
            for uid in list(ws._connections.keys()):
                await physical.on_time_of_day(uid, hour)
        except Exception as e:
            logger.debug("Physical time-of-day update failed: %s", e)
        await self._deliver(self._pick_message(MORNING_MESSAGES))

    async def _daily_challenge(self) -> None:
        """Issue a daily challenge to the Commander."""
        if not self._on_message_callback:
            return
        if self._affection_level < 2:
            return  # Don't challenge a stranger

        try:
            from ..personality import load_personality
            p = load_personality()
            challenges = p.get("daily_challenges", {}).get("challenges", [])
            if not challenges:
                return

            import random
            challenge = random.choice(challenges)
            await self._on_message_callback(challenge["prompt"])
            logger.info("Daily challenge issued: %s", challenge["type"])
        except Exception as e:
            logger.warning("Daily challenge failed: %s", e)

    async def _evening_checkin(self) -> None:
        # Update physical state for late watch for all connected users
        try:
            from ..context import physical, ws
            hour = datetime.now().hour
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
