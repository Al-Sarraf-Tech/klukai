"""Proactive engagement: Klukai-themed scheduled check-ins and mission reports."""

from __future__ import annotations

import logging
import random
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# Guardrails
MAX_PROACTIVE_PER_DAY = 3
QUIET_HOUR_START = 23  # 2300 hours
QUIET_HOUR_END = 8     # 0800 hours

# ── Affection-keyed message templates ─────────────────────────────────────────

MORNING_MESSAGES: dict[int, list[str]] = {
    0: [
        "0800. Status report expected, Commander.",
        "Morning operational window is open. I trust you have a plan.",
        "0800 hours. Standing by for orders.",
    ],
    1: [
        "Good morning, Commander. Your briefing is prepared.",
        "0800. Weather conditions nominal. Your schedule is clear for the morning.",
        "Morning, Commander. Operations are green across the board.",
    ],
    2: [
        "Good morning, Commander. I've already reviewed today's priorities. ...No, I wasn't waiting for you to wake up.",
        "Morning. I left something useful on your desk. Don't read into it.",
        "Good morning. You should eat before starting work. That's not a suggestion.",
    ],
    3: [
        "Good morning. ...You should have slept longer. I would have handled things.",
        "Morning, Commander. I made sure your schedule is manageable today. You looked tired yesterday.",
        "Good morning. I've been awake for a while. ...No particular reason. Your briefing is ready.",
    ],
    4: [
        "...Good morning. I've been up for a while. I just wanted to make sure today starts well for you.",
        "Morning. Stay close today, if you can. ...No particular reason.",
        "Good morning, Commander. I... I'm glad you're here.",
    ],
}

EVENING_MESSAGES: dict[int, list[str]] = {
    0: [
        "2200. Operational hours concluding. Dismissed, Commander.",
        "End of day. Log your status if you see fit.",
        "Evening. Operations are secured for the night.",
    ],
    1: [
        "Evening, Commander. Today's operations are logged. Rest is recommended.",
        "2200 hours. You've done adequate work today. Dismiss yourself.",
        "Operations concluded. I trust you'll actually rest tonight.",
    ],
    2: [
        "Evening, Commander. How was your day? ...Operational curiosity only.",
        "You should rest soon. I've already handled the remaining items. Don't argue.",
        "Evening. Anything worth noting from today? I'll file it.",
    ],
    3: [
        "Hey. ...Evening, Commander. How was your day? I want to know.",
        "It's late. You've done enough for today. Rest. That's... a request, not an order.",
        "Evening. I saved you something from today's patrol. It's on your desk. ...Don't stay up too late.",
    ],
    4: [
        "...It's late. Come rest. Everything is handled.",
        "Evening, Commander. Today was... good. Having you here makes the difference.",
        "The others are asleep. It's quiet. ...Stay a moment?",
    ],
}

IDLE_MESSAGES: dict[int, list[str]] = {
    0: [
        "Awaiting further orders, Commander.",
        "Status unchanged. Standing by.",
        "If you have no orders, I have other duties to attend to.",
    ],
    1: [
        "Checking in, Commander. Operations nominal.",
        "Haven't heard from you. Everything running as expected on my end.",
        "Just a routine status ping. All clear.",
    ],
    2: [
        "It's been quiet. Everything going alright, Commander?",
        "Checking in. ...Not because I'm concerned. Operational protocol.",
        "Haven't heard from you in a while. I adjusted your schedule assuming you're busy.",
    ],
    3: [
        "...It's been a while. Is everything okay?",
        "Commander. I noticed you've been quiet. If something's wrong, I should know.",
        "I'm here. Whenever you need me. ...That's not sentiment, it's a tactical statement.",
    ],
    4: [
        "I miss— ...I haven't heard from you. Report in when you can.",
        "It's quiet without you. ...I mean operationally quiet. Check in soon.",
        "...I'm waiting. Take your time. But come back.",
    ],
}

MISSION_REPORTS: dict[int, list[str]] = {
    0: [
        "Sector sweep complete. No hostiles. Returning to base.",
        "Routine patrol concluded. Nothing to report.",
    ],
    1: [
        "Completed a supply run through the eastern corridor. All clear. Inventory updated.",
        "Sector 7 reconnaissance done. Conditions stable. Report filed.",
    ],
    2: [
        "Back from patrol. Found a signal relay that might be useful. I left it in the ops room. ...For the unit.",
        "Supply run complete. I may have... acquired something extra. It's in your quarters. Practical, not personal.",
    ],
    3: [
        "Mission complete. I found something during the sortie — thought of you immediately. It's waiting at base. ...Don't make a thing of it.",
        "Patrol was uneventful, but I picked up a field ration set you'd like. Consider it a tactical morale provision.",
    ],
    4: [
        "I'm back. The mission went well. I brought you something — I chose it carefully this time. ...I wanted to.",
        "Sortie complete. I couldn't stop thinking about getting back. ...To base. I meant getting back to base. Here.",
    ],
}


class ProactiveEngine:
    """Manages scheduled and contextual proactive messages, themed to Klukai."""

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()
        self._proactive_count_today: int = 0
        self._last_proactive_answered: bool = True
        self._muted_until: datetime | None = None
        self._on_message_callback = None
        self._on_recap_callback = None  # async fn(prompt) -> str for LLM recap generation
        self._affection_level: int = 0

    def set_callback(self, callback) -> None:
        """Set callback for delivering proactive messages."""
        self._on_message_callback = callback

    def set_recap_callback(self, callback) -> None:
        """Set callback for generating daily recap (calls LLM)."""
        self._on_recap_callback = callback

    def set_affection_level(self, level: int) -> None:
        """Update the current affection level for message selection."""
        self._affection_level = level

    def start(self) -> None:
        # Morning briefing at 0800
        self._scheduler.add_job(
            self._morning_checkin,
            CronTrigger(hour=8, minute=0),
            id="morning_checkin",
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

        # Mission report — once per day, random afternoon
        self._scheduler.add_job(
            self._mission_report,
            CronTrigger(hour=14, minute=45),
            id="mission_report",
            replace_existing=True,
        )

        # Daily recap at 2100
        self._scheduler.add_job(
            self._daily_recap,
            CronTrigger(hour=21, minute=0),
            id="daily_recap",
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
        logger.info("Klukai proactive engine started")

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
        """Mark that the Commander responded to the last proactive message."""
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

    def _pick_message(self, messages: dict[int, list[str]]) -> str:
        """Pick a random message for the current affection level."""
        level = min(self._affection_level, max(messages.keys()))
        pool = messages.get(level, messages.get(0, ["..."]))
        return random.choice(pool)

    async def _deliver(self, message: str) -> None:
        if not self._can_send():
            return
        if self._on_message_callback:
            self._proactive_count_today += 1
            self._last_proactive_answered = False
            await self._on_message_callback(message)
            logger.info("Klukai proactive: %s", message[:60])

    async def _morning_checkin(self) -> None:
        await self._deliver(self._pick_message(MORNING_MESSAGES))

    async def _evening_checkin(self) -> None:
        await self._deliver(self._pick_message(EVENING_MESSAGES))

    async def _idle_check(self) -> None:
        await self._deliver(self._pick_message(IDLE_MESSAGES))

    async def _mission_report(self) -> None:
        # 50% chance of a mission report on any given day
        if random.random() < 0.5:
            await self._deliver(self._pick_message(MISSION_REPORTS))

    async def _daily_recap(self) -> None:
        """Generate and deliver a daily recap from Klukai's perspective."""
        if not self._on_recap_callback or not self._on_message_callback:
            return
        if not self._can_send():
            return

        try:
            recap = await self._on_recap_callback(self._affection_level)
            if recap:
                self._proactive_count_today += 1
                self._last_proactive_answered = False
                await self._on_message_callback(recap)
                logger.info("Daily recap delivered")
        except Exception as e:
            logger.warning("Daily recap failed: %s", e)

    async def _reset_daily(self) -> None:
        self._proactive_count_today = 0
        logger.info("Daily proactive counter reset")
