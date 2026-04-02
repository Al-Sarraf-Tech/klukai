"""Proactive engagement: scheduled check-ins, reminders, conversation starters."""

from __future__ import annotations

import logging
import os
import random
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# Guardrails
MAX_PROACTIVE_PER_DAY = 3
QUIET_HOUR_START = 23  # 11pm
QUIET_HOUR_END = 8  # 8am

MORNING_STARTERS = [
    "Good morning! How are you feeling today?",
    "Morning! Got any interesting plans for today?",
    "Hey, good morning! Ready to take on the day?",
    "Rise and shine! What's on the agenda today?",
]

EVENING_STARTERS = [
    "Hey, winding down for the night? How was your day?",
    "Evening! Anything interesting happen today?",
    "Hey there — how'd your day go?",
]

IDLE_STARTERS = [
    "Hey! Just checking in — doing anything fun?",
    "It's been quiet — everything going alright?",
    "Hey, thought of something interesting. Want to chat?",
    "Haven't heard from you in a while — all good?",
]


class ProactiveEngine:
    """Manages scheduled and contextual proactive messages."""

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()
        self._proactive_count_today: int = 0
        self._last_proactive_answered: bool = True
        self._muted_until: datetime | None = None
        self._on_message_callback = None

    def set_callback(self, callback) -> None:
        """Set callback for delivering proactive messages."""
        self._on_message_callback = callback

    def start(self) -> None:
        # Morning check-in at 8am
        self._scheduler.add_job(
            self._morning_checkin,
            CronTrigger(hour=8, minute=0),
            id="morning_checkin",
            replace_existing=True,
        )

        # Evening wind-down at 10pm
        self._scheduler.add_job(
            self._evening_checkin,
            CronTrigger(hour=22, minute=0),
            id="evening_checkin",
            replace_existing=True,
        )

        # Idle check every 2 hours during waking hours
        self._scheduler.add_job(
            self._idle_check,
            CronTrigger(hour="9-21/2", minute=30),
            id="idle_check",
            replace_existing=True,
        )

        # Reset daily counter at midnight
        self._scheduler.add_job(
            self._reset_daily,
            CronTrigger(hour=0, minute=0),
            id="daily_reset",
            replace_existing=True,
        )

        self._scheduler.start()
        logger.info("Proactive engine started")

    def stop(self) -> None:
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
        """Mark that the user responded to the last proactive message."""
        self._last_proactive_answered = True

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

    async def _deliver(self, message: str) -> None:
        if not self._can_send():
            return
        if self._on_message_callback:
            self._proactive_count_today += 1
            self._last_proactive_answered = False
            await self._on_message_callback(message)
            logger.info("Proactive sent: %s", message[:50])

    async def _morning_checkin(self) -> None:
        await self._deliver(random.choice(MORNING_STARTERS))

    async def _evening_checkin(self) -> None:
        await self._deliver(random.choice(EVENING_STARTERS))

    async def _idle_check(self) -> None:
        # Only send if it's been a while since last message
        # The callback handler should check actual idle time
        await self._deliver(random.choice(IDLE_STARTERS))

    async def _reset_daily(self) -> None:
        self._proactive_count_today = 0
        logger.info("Daily proactive counter reset")
