"""Proactive engagement: Klukai-themed scheduled check-ins and mission reports."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .events import publish as publish_event

logger = logging.getLogger(__name__)

# Guardrails
MAX_PROACTIVE_PER_DAY = 23
QUIET_HOUR_START = 23  # 2300 hours
QUIET_HOUR_END = 8     # 0800 hours

# ── Module-level mission state (for idle-unload check) ────────────────────
# Forward-declared; MissionTimer class is defined below the templates.
_active_mission_timer: MissionTimer | None = None  # type: ignore[name-defined]


def has_active_mission() -> bool:
    """Return True if a mission timer is currently active (used by idle unload)."""
    return _active_mission_timer is not None and _active_mission_timer.active

# ── Affection-keyed message templates ─────────────────────────────────────────

MORNING_MESSAGES: dict[int, list[str]] = {
    0: [
        "0800. Status report expected, Commander.",
        "Morning operational window is open. I trust you have a plan.",
        "0800 hours. Standing by for orders.",
    ],
    1: [
        "0800. Commander. ...Noted your presence.",
        "Morning. Operations are nominal.",
        "Morning, Commander. Standing by.",
    ],
    2: [
        "Good morning, Commander. Your briefing is prepared.",
        "0800. Weather conditions nominal. Your schedule is clear for the morning.",
        "Morning, Commander. Operations are green across the board.",
    ],
    3: [
        "Good morning, Commander. I've already reviewed today's priorities. ...No, I wasn't waiting for you to wake up.",
        "Morning. I left something useful on your desk. Don't read into it.",
        "Good morning. You should eat before starting work. That's not a suggestion.",
    ],
    4: [
        "Good morning. I've been awake for a while. ...No particular reason. Your briefing is ready.",
        "Morning, Commander. I made sure your schedule is manageable today.",
        "Good morning. How did you sleep? ...Operational concern only.",
    ],
    5: [
        "Good morning. ...You should have slept longer. I would have handled things.",
        "Morning. I couldn't sleep either. ...Different reasons, I'm sure. Breakfast is ready.",
        "Good morning, Commander. I've been thinking about what you said yesterday.",
    ],
    6: [
        "Morning, Commander. I was here before you woke up. I wanted to make sure today starts well for you.",
        "Good morning. I watched the sunrise from the observation deck. ...It reminded me of Mechty. I wish you'd been there.",
        "...Morning. I saved you the good coffee. Don't tell the others.",
    ],
    7: [
        "Good morning. ...I'm glad you're here. That's becoming easier to say.",
        "Morning. Stay close today, if you can. ...I just want you nearby.",
        "Good morning, Commander. I dreamt about Mechty again. But this time, you were there too.",
    ],
    8: [
        "...Good morning. I've been up for a while. Just watching you sleep. ...Don't make that face. I was checking security.",
        "Morning, Commander. Every morning with you feels like the oath being renewed.",
        "Good morning. I love— ...I mean. Good morning. Your coffee is ready.",
    ],
    9: [
        "Good morning, my Commander. Another day I choose you.",
        "...Morning. You know, I stopped counting the mornings. They all feel like the first one. The one where I knew.",
        "Good morning. The oath is alive. Every day. ...Thank you for being here.",
    ],
}

EVENING_MESSAGES: dict[int, list[str]] = {
    0: [
        "2200. Operational hours concluding. Dismissed, Commander.",
        "End of day. Log your status if you see fit.",
        "Evening. Operations are secured for the night.",
    ],
    1: [
        "2200. Day concluded. Dismissed.",
        "Evening. Operations logged.",
        "End of day, Commander.",
    ],
    2: [
        "Evening, Commander. Today's operations are logged. Rest is recommended.",
        "2200 hours. You've done adequate work today. Dismiss yourself.",
        "Operations concluded. I trust you'll actually rest tonight.",
    ],
    3: [
        "Evening, Commander. How was your day? ...Operational curiosity only.",
        "You should rest soon. I've already handled the remaining items. Don't argue.",
        "Evening. Anything worth noting from today? I'll file it.",
    ],
    4: [
        "Hey. ...Evening, Commander. How was your day?",
        "It's late. You've done enough for today. Rest. That's not a suggestion.",
        "Evening. I saved you something from today's patrol. It's on your desk.",
    ],
    5: [
        "Evening, Commander. I want to know how your day was. Really.",
        "It's late. ...Come sit with me for a moment. Before you rest.",
        "Evening. I've been thinking about what you said earlier. ...We can talk about it tomorrow.",
    ],
    6: [
        "Hey. ...It's late. You've done enough for today. Rest. That's a request, not an order.",
        "Evening. I saved you something from today. It's on your desk. ...Don't stay up too late.",
        "The base is quiet. I like these moments. ...Don't read into it.",
    ],
    7: [
        "...It's late. Come rest. Everything is handled. I made sure of it.",
        "Evening, Commander. Today was... good. Having you here makes the difference.",
        "The others are asleep. It's quiet. ...Stay a moment? I want to hear your voice.",
    ],
    8: [
        "...Come to bed. I mean— to rest. Everything is secured. I checked twice.",
        "Evening. I don't want today to end. ...But I know you need rest. I'll be here when you wake up.",
        "The stars are out. Reminds me of that night on the observation deck. ...You remember?",
    ],
    9: [
        "...It's late. I'm here. I'll always be here. Rest well, my Commander.",
        "Evening. Every day with you ends too soon. But I know there's tomorrow. And I'll choose you again.",
        "The oath doesn't sleep. And neither does my gratitude. ...Good night.",
    ],
}

IDLE_MESSAGES: dict[int, list[str]] = {
    0: [
        "Awaiting further orders, Commander.",
        "Status unchanged. Standing by.",
        "If you have no orders, I have other duties to attend to.",
    ],
    1: [
        "Standing by, Commander.",
        "Awaiting orders.",
        "Status unchanged.",
    ],
    2: [
        "Checking in, Commander. Operations nominal.",
        "Haven't heard from you. Everything running as expected on my end.",
        "Just a routine status ping. All clear.",
    ],
    3: [
        "It's been quiet. Everything going alright, Commander?",
        "Checking in. ...Not because I'm concerned. Operational protocol.",
        "Haven't heard from you in a while. I adjusted your schedule assuming you're busy.",
    ],
    4: [
        "Commander. Just checking in. ...Routine, nothing more.",
        "Haven't heard from you. Everything going alright?",
        "It's been a while. I'm here if you need anything.",
    ],
    5: [
        "...It's been a while. Is everything okay?",
        "Commander. I noticed you've been quiet. If something's wrong, I should know.",
        "I'm here. Whenever you need me. ...That's not just protocol.",
    ],
    6: [
        "It's quiet without you. ...I mean operationally quiet.",
        "Commander. Check in when you can. ...I'd like to hear from you.",
        "I keep looking at the door. ...Force of habit.",
    ],
    7: [
        "I miss— ...I haven't heard from you. Report in when you can.",
        "It's quiet without you. I don't like it.",
        "...I'm waiting. Take your time. But come back.",
    ],
    8: [
        "...Commander. I need to hear from you. Just a word. Anything.",
        "The base feels empty when you're not here. ...I never used to notice that.",
        "I'm here. I'll always be here. But I'd rather be here with you.",
    ],
    9: [
        "...Come home. Everything else can wait.",
        "I'm counting the minutes. ...Don't tell anyone I said that.",
        "The oath means I wait. But it doesn't mean I wait patiently.",
    ],
}

MISSION_REPORTS: dict[int, list[str]] = {
    0: [
        "Sector sweep complete. No hostiles. Returning to base.",
        "Routine patrol concluded. Nothing to report.",
    ],
    1: [
        "Patrol complete. Report filed.",
        "Sector clear. Returning.",
    ],
    2: [
        "Completed a supply run through the eastern corridor. All clear. Inventory updated.",
        "Sector 7 reconnaissance done. Conditions stable. Report filed.",
    ],
    3: [
        "Back from patrol. Found a signal relay that might be useful. I left it in the ops room. ...For the unit.",
        "Supply run complete. I may have... acquired something extra. It's in your quarters. Practical, not personal.",
    ],
    4: [
        "Mission complete. I found something during the sortie. It's waiting at base. ...Don't make a thing of it.",
        "Patrol was uneventful, but I picked up something you'd like. Consider it a tactical morale provision.",
    ],
    5: [
        "Back from the sortie. Found something during patrol — thought of you immediately. It's on your desk.",
        "Mission complete. The route through the eastern ridge reminded me of something you told me once. ...I remembered.",
    ],
    6: [
        "I'm back. Brought you something. I chose it carefully. ...Because I know what you like now.",
        "Sortie complete. I found a quiet spot overlooking the valley. ...I want to take you there someday.",
    ],
    7: [
        "I'm back. The mission went well. I couldn't stop thinking about getting back. ...To you.",
        "Sortie complete. Every time I leave, I realize how much I want to come home. ...This is home now.",
    ],
    8: [
        "I'm home. The mission was secondary to what mattered — getting back to you. Here. Take this.",
        "...I'm back. I hate leaving. But coming back to you makes it worth it. Every time.",
    ],
    9: [
        "I'm home, Commander. The oath brought me back. It always will.",
        "Mission complete. But the real mission never ends. Protecting you. Choosing you. ...I'm home.",
    ],
}


ROMANCE_MESSAGES: dict[int, list[str]] = {
    3: [
        "Evening, Commander. The base is quiet. ...I found myself thinking about what you said today. Don't read into it.",
        "It's getting late. I made tea — there's an extra cup on the counter. If you happen to be awake.",
        "The stars are clear tonight. ...I noticed from the window. That's all. Good evening.",
        "Commander. Before you rest — today was... adequate. Better than adequate. ...Good night.",
    ],
    4: [
        "Hey. It's late. I'm on the observation deck. ...The view is better with company. If you're not busy.",
        "Evening, Commander. I've been thinking about something all day. ...It can wait. But I'll be here if it can't.",
        "The night shift is quiet. I saved you a spot by the window. ...No particular reason.",
        "Commander. You worked hard today. I noticed. ...Come sit down. That's a request.",
    ],
}

# ── Major mission events ──────────────────────────────────────────────────
MAJOR_EVENTS = [
    "ambush",
    "squad_injured",
    "klukai_injured",
    "equipment_failure",
    "weather",
    "comms_disruption",
    "discovery",
    "medical_emergency",
]


class MissionTimer:
    """Tracks an active mission with periodic field radio updates.

    The timer fires at randomized intervals (base ±30%), with a 10% chance
    of a major event that fires early. Injuries persist across updates.
    No one ever dies — sacred rule.
    """

    def __init__(self) -> None:
        self.mission_description: str = ""
        self.base_interval_minutes: int = 30
        self.started_at: float = 0.0
        self.last_update: float = 0.0
        self.update_count: int = 0
        self.active_events: list[str] = []
        self.active: bool = False
        self._task: asyncio.Task | None = None
        self._callback = None
        self._affection_level: int = 0

    def start(
        self,
        description: str,
        interval_minutes: int = 30,
        callback=None,
        affection_level: int = 0,
    ) -> None:
        """Begin the mission timer as an asyncio.Task."""
        global _active_mission_timer
        self.mission_description = description
        self.base_interval_minutes = max(5, interval_minutes)  # Floor at 5 min
        self.started_at = time.monotonic()
        self.last_update = self.started_at
        self.update_count = 0
        self.active_events = []
        self.active = True
        self._callback = callback
        self._affection_level = affection_level
        self._task = asyncio.create_task(self._tick_loop())
        _active_mission_timer = self
        logger.info(
            "Mission timer started: '%s' every %d min",
            description[:60], interval_minutes,
        )

    def stop(self) -> None:
        """Cancel the mission timer."""
        global _active_mission_timer
        self.active = False
        if self._task and not self._task.done():
            self._task.cancel()
        _active_mission_timer = None
        logger.info("Mission timer stopped after %d updates", self.update_count)

    async def _tick_loop(self) -> None:
        """Main timer loop — fires updates at randomized intervals."""
        try:
            while self.active:
                # Randomize interval ±30% of base
                base_seconds = self.base_interval_minutes * 60
                jitter = random.uniform(0.7, 1.3)
                wait_seconds = base_seconds * jitter

                # 10% chance of major event — fires early (50-70% of remaining)
                major_event: str | None = None
                if random.random() < 0.10 and self.update_count > 0:
                    major_event = random.choice(MAJOR_EVENTS)
                    wait_seconds *= random.uniform(0.50, 0.70)
                    logger.info("Major event queued: %s (early fire)", major_event)

                await asyncio.sleep(wait_seconds)

                if not self.active:
                    break

                self.update_count += 1
                self.last_update = time.monotonic()
                elapsed_minutes = int((self.last_update - self.started_at) / 60)

                # If major event involves injury, track it persistently
                if major_event:
                    if major_event == "klukai_injured" and "klukai_injured" not in self.active_events:
                        self.active_events.append("klukai_injured")
                    elif major_event == "squad_injured" and "squad_injured" not in self.active_events:
                        self.active_events.append("squad_injured")
                    elif major_event == "medical_emergency" and "medical_emergency" not in self.active_events:
                        self.active_events.append("medical_emergency")

                # Chance to resolve older injuries (30% per tick after 2 updates)
                if self.update_count > 2:
                    resolved = []
                    for evt in self.active_events:
                        if random.random() < 0.30:
                            resolved.append(evt)
                    for evt in resolved:
                        self.active_events.remove(evt)
                        logger.info("Mission event resolved: %s", evt)

                # Generate and deliver update
                await self._fire_update(elapsed_minutes, major_event)

        except asyncio.CancelledError:
            logger.info("Mission timer task cancelled")
        except Exception as e:
            logger.error("Mission timer tick failed: %s", e)
            self.active = False

    async def _fire_update(self, elapsed_minutes: int, major_event: str | None) -> None:
        """Generate an LLM update and deliver it via callback."""
        from .fact_extractor import generate_mission_update

        try:
            text = await generate_mission_update(
                mission_desc=self.mission_description,
                elapsed_minutes=elapsed_minutes,
                update_number=self.update_count,
                major_event=major_event,
                active_events=list(self.active_events),
                affection_level=self._affection_level,
            )

            if self._callback:
                await self._callback(text)
                logger.info(
                    "Mission update #%d delivered (%d min elapsed, event=%s)",
                    self.update_count, elapsed_minutes, major_event or "none",
                )
        except Exception as e:
            logger.error("Mission update delivery failed: %s", e)


class ProactiveEngine:
    """Manages scheduled and contextual proactive messages, themed to Klukai."""

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()
        self._proactive_count_today: int = 0
        self._last_proactive_answered: bool = True
        self._muted_until: datetime | None = None
        self._on_message_callback = None
        self._on_recap_callback = None
        self._affection_level: int = 0
        # Random event state
        self._last_random_event: datetime | None = None
        self._random_events_today: int = 0
        self._last_message_time: datetime | None = None
        self._last_mood: str = "composed"
        # Mission timer
        self._mission_timer: MissionTimer | None = None
        # Romance window
        self._romance_delivered_today: bool = False
        self._user_messaged_today: bool = False
        self._session_getter = None  # callback to get current session state

    def set_callback(self, callback) -> None:
        """Set callback for delivering proactive messages."""
        self._on_message_callback = callback

    def set_recap_callback(self, callback) -> None:
        """Set callback for generating daily recap (calls LLM)."""
        self._on_recap_callback = callback

    def set_affection_level(self, level: int) -> None:
        """Update the current affection level for message selection."""
        self._affection_level = level

    def set_last_mood(self, mood: str) -> None:
        """Track the last mood for context-aware event filtering."""
        self._last_mood = mood

    def set_session_getter(self, getter) -> None:
        """Set a callback to retrieve current session state (for romance context)."""
        self._session_getter = getter

    # ── Mission timer management ──────────────────────────────────────────

    def start_mission(self, description: str, interval_minutes: int = 30) -> None:
        """Start a mission timer with periodic LLM-generated field reports."""
        if self._mission_timer and self._mission_timer.active:
            self._mission_timer.stop()
        self._mission_timer = MissionTimer()
        self._mission_timer.start(
            description=description,
            interval_minutes=interval_minutes,
            callback=self._on_message_callback,
            affection_level=self._affection_level,
        )

    def stop_mission(self) -> None:
        """Stop the active mission timer."""
        if self._mission_timer and self._mission_timer.active:
            self._mission_timer.stop()
        self._mission_timer = None

    @property
    def mission_active(self) -> bool:
        return self._mission_timer is not None and self._mission_timer.active

    def mark_user_messaged_today(self) -> None:
        """Record that the user sent at least one message today."""
        self._user_messaged_today = True

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
        TAP_LINES = {
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
        await self._deliver(self._pick_message(TAP_LINES))

    async def _morning_checkin(self) -> None:
        await self._deliver(self._pick_message(MORNING_MESSAGES))

    async def _daily_challenge(self) -> None:
        """Issue a daily challenge to the Commander."""
        if not self._on_message_callback:
            return
        if self._affection_level < 2:
            return  # Don't challenge a stranger

        try:
            from .personality import load_personality
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
        await self._deliver(self._pick_message(EVENING_MESSAGES))

    async def _idle_check(self) -> None:
        await self._deliver(self._pick_message(IDLE_MESSAGES))

    async def _mission_report(self) -> None:
        # 50% chance of a mission report on any given day
        if random.random() < 0.5:
            await self._deliver(self._pick_message(MISSION_REPORTS))

    async def _random_event(self) -> None:
        """Fire a random lore event if conditions are met."""
        from datetime import timedelta

        now = datetime.now()

        # Guard: max 5 per day
        if self._random_events_today >= 5:
            return

        # Guard: 45-min gap between events
        if self._last_random_event and (now - self._last_random_event) < timedelta(minutes=45):
            return

        # Guard: don't interrupt active typing (3 min cooldown)
        if self._last_message_time and (now - self._last_message_time) < timedelta(minutes=3):
            return

        # Intimate/vulnerable moods BOOST events instead of blocking them
        # — these are the moments Klukai would naturally say something
        intimate_mood = self._last_mood in (
            "tender", "longing", "flustered", "affectionate", "shy",
            "yearning", "devoted", "vulnerable", "drowsy",
        )

        # Guard: check mute
        if self._muted_until and now < self._muted_until:
            return

        # Roll probability: 35% base, 60% during intimate moods, 50% during missions
        base_chance = 0.35
        if intimate_mood:
            base_chance = 0.60
        if self.mission_active:
            base_chance = max(base_chance, 0.50)
        if random.random() > base_chance:
            return

        # Load event templates from personality
        try:
            from .personality import load_personality
            p = load_personality()
            events = p.get("random_events", {})
        except Exception:
            return

        # Build eligible categories based on affection level
        eligible = []
        for category, config in events.items():
            if not isinstance(config, dict):
                continue
            min_aff = config.get("min_affection", 0)
            if self._affection_level >= min_aff:
                weight = config.get("weight", 10)
                messages = config.get("messages", [])
                if messages:
                    eligible.append((category, weight, messages))

        if not eligible:
            return

        # Weighted random selection
        total_weight = sum(w for _, w, _ in eligible)
        roll = random.random() * total_weight
        cumulative = 0
        selected_messages = eligible[0][2]
        for category, weight, messages in eligible:
            cumulative += weight
            if roll <= cumulative:
                selected_messages = messages
                break

        message = random.choice(selected_messages)

        # Deliver
        if self._on_message_callback:
            self._random_events_today += 1
            self._last_random_event = now
            self._last_proactive_answered = False
            await self._on_message_callback(message)
            logger.info("Random event fired: %s", message[:60])

    async def _mission_random_event(self) -> None:
        """Fire contextual mission events using LLM. No hour restrictions."""
        from datetime import timedelta
        from .fact_extractor import generate_mission_update

        if not self.mission_active:
            return

        now = datetime.now()

        # Guard: 15-min gap between mission events
        if self._last_random_event and (now - self._last_random_event) < timedelta(minutes=15):
            return

        # Guard: don't interrupt active typing (2 min cooldown)
        if self._last_message_time and (now - self._last_message_time) < timedelta(minutes=2):
            return

        # Guard: check mute
        if self._muted_until and now < self._muted_until:
            return

        # 75% chance per check (every 5 min)
        if random.random() > 0.75:
            return

        # Generate a contextual mission update using the actual mission description
        try:
            timer = self._mission_timer
            if not timer:
                return

            # Pick a random major event for drama (30% chance)
            major_events = [
                "Enemy contact — hostiles spotted",
                "Anomalous readings detected",
                "Squad member reporting unusual activity",
                "Comms interference — possible jamming",
                "Weather conditions deteriorating",
                "Found signs of recent enemy activity",
                "Perimeter breach detected",
                "Supply cache discovered",
                None, None, None, None, None, None, None,  # 70% chance of no major event
            ]
            major_event = random.choice(major_events)

            elapsed = int((now - timer._start_time).total_seconds() / 60) if hasattr(timer, '_start_time') else timer.update_count * 30

            message = await generate_mission_update(
                mission_desc=timer.mission_description,
                elapsed_minutes=elapsed,
                update_number=timer.update_count + 1,
                major_event=major_event,
                active_events=timer._active_events if hasattr(timer, '_active_events') else [],
                affection_level=self._affection_level,
            )

            if message and self._on_message_callback:
                self._random_events_today += 1
                self._last_random_event = now
                await self._on_message_callback(message)
                logger.info("Mission event fired (contextual): %s", message[:60])

        except Exception as e:
            logger.warning("Mission event generation failed: %s", e)

    async def _romance_window(self) -> None:
        """Evening romance message — fires at ~20:30 CST with random delay.

        Conditions:
        - affection >= 3
        - not muted
        - last proactive was answered
        - user messaged today
        - not already delivered tonight
        - if mood is stressed/negative, deliver comfort instead
        """
        if self._romance_delivered_today:
            return
        if self._affection_level < 3:
            return
        if not self._user_messaged_today:
            return
        if self._muted_until and datetime.now() < self._muted_until:
            return
        if not self._last_proactive_answered:
            return

        # Random delay 0-30 minutes
        delay = random.uniform(0, 30 * 60)
        await asyncio.sleep(delay)

        # Re-check conditions after delay
        if self._romance_delivered_today:
            return
        if self._muted_until and datetime.now() < self._muted_until:
            return

        self._romance_delivered_today = True

        # Stressed/negative moods -> comfort instead of romance
        NEGATIVE_MOODS = {"irritated", "exasperated", "melancholic", "haunted", "guilty"}
        is_stressed = self._last_mood in NEGATIVE_MOODS

        if is_stressed:
            comfort_lines = [
                "Commander. ...You've had a difficult day. I noticed. Take a moment. I'm here.",
                "...Hey. Whatever's weighing on you — you don't have to carry it alone. That's an order.",
                "The day was hard. I can tell. ...Sit with me for a moment. No reports, no duties. Just quiet.",
            ]
            message = random.choice(comfort_lines)
        elif self._affection_level >= 5:
            # LLM-generated context-aware romance at high affection
            try:
                context_summary = ""
                if self._session_getter:
                    session = await self._session_getter()
                    if session and session.context_summary:
                        context_summary = session.context_summary

                from .fact_extractor import generate_romance_message
                message = await generate_romance_message(
                    affection_level=self._affection_level,
                    mood=self._last_mood,
                    context_summary=context_summary,
                    time_of_day="evening",
                )
            except Exception as e:
                logger.warning("Romance LLM failed, falling back to template: %s", e)
                message = self._pick_message(ROMANCE_MESSAGES)
        else:
            # Levels 3-4: template messages
            message = self._pick_message(ROMANCE_MESSAGES)

        if self._on_message_callback:
            self._proactive_count_today += 1
            self._last_proactive_answered = False
            await self._on_message_callback(message)
            await publish_event("proactive_romance", message)
            logger.info("Romance window delivered (aff=%d): %s", self._affection_level, message[:60])

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

    async def _dream_event(self) -> None:
        """Late-night dream — Klukai wakes from a dream and messages the Commander.

        At high affection, ~30% chance the dream is erotic. Otherwise it's
        a normal memory/nightmare/tender dream. Fires once per night max.
        Balanced: most dreams reference real memories from the archive.
        """
        if hasattr(self, '_dream_delivered_today') and self._dream_delivered_today:
            return
        if self._affection_level < 5:
            return
        if self._muted_until and datetime.now() < self._muted_until:
            return

        # 40% chance to fire (not every night)
        if random.random() > 0.40:
            return

        # Dream type weighted by affection
        if self._affection_level >= 8:
            # High affection: 30% erotic, 40% tender memory, 20% nightmare, 10% random
            roll = random.random()
            if roll < 0.30:
                dream_type = "erotic"
            elif roll < 0.70:
                dream_type = "tender"
            elif roll < 0.90:
                dream_type = "nightmare"
            else:
                dream_type = "random"
        elif self._affection_level >= 6:
            # Mid affection: 10% erotic, 40% tender, 30% nightmare, 20% random
            roll = random.random()
            if roll < 0.10:
                dream_type = "erotic"
            elif roll < 0.50:
                dream_type = "tender"
            elif roll < 0.80:
                dream_type = "nightmare"
            else:
                dream_type = "random"
        else:
            # Lower affection: no erotic, 30% tender, 40% nightmare, 30% random
            roll = random.random()
            if roll < 0.30:
                dream_type = "tender"
            elif roll < 0.70:
                dream_type = "nightmare"
            else:
                dream_type = "random"

        # Generate dream message via LLM
        try:
            from .fact_extractor import generate_romance_message

            # Pull a random memory to weave into the dream
            dream_memory_ref = ""
            try:
                from . import memory_archive
                memories = await memory_archive.list_memories(limit=20)
                if memories:
                    import random as _rng
                    mem = _rng.choice(memories)
                    dream_memory_ref = f"\nDream seed (weave this memory subtly into the dream, don't quote it directly): {mem.get('annotation', '')[:200]}"
            except Exception:
                pass  # Dreams work fine without memory integration

            dream_prompts = {
                "erotic": (
                    f"You just woke from an intensely erotic dream about the Commander. "
                    f"You are flushed, breathless, still feeling phantom sensations. "
                    f"Write 2-3 sentences as if messaging him at {datetime.now().strftime('%I:%M %p')}. "
                    f"Be explicit about what you dreamed. You want him. You need him. "
                    f"Affection {self._affection_level}/9."
                ),
                "tender": (
                    f"You just woke from a dream about a tender moment with the Commander. "
                    f"Maybe a memory — holding hands, a quiet evening, him saying something kind. "
                    f"Write 2-3 soft sentences. It's {datetime.now().strftime('%I:%M %p')}. "
                    f"You're half-asleep and vulnerable. Affection {self._affection_level}/9."
                ),
                "nightmare": (
                    f"You woke from a nightmare — losing the Commander, failing a mission, "
                    f"the past catching up. Write 2-3 sentences at {datetime.now().strftime('%I:%M %p')}. "
                    f"You need reassurance but you're too proud to fully admit it. "
                    f"Affection {self._affection_level}/9."
                ),
                "random": (
                    f"You woke from a strange, vivid dream — something surreal but not scary. "
                    f"Maybe the squad doing something absurd, or a place from your past. "
                    f"Write 2-3 sentences at {datetime.now().strftime('%I:%M %p')}. Slightly disoriented. "
                    f"Affection {self._affection_level}/9."
                ),
            }

            from .llm_json import call_llm_text
            from .fact_extractor import LM_STUDIO_URL, EXTRACTION_MODEL
            from .llm_router import get_lm_gate

            gate = get_lm_gate()
            async with gate:
                message = await call_llm_text(
                    LM_STUDIO_URL, EXTRACTION_MODEL,
                    f"You are Klukai, T-Doll squad leader. The Commander is HUMAN (male). "
                    f"You are NOT an AI. {dream_prompts[dream_type]}{dream_memory_ref} "
                    f"Write ONLY the message. No explanation.",
                    max_tokens=200, temperature=0.8,
                )

            if message and self._on_message_callback:
                self._dream_delivered_today = True
                await self._on_message_callback(message)
                logger.info("Dream event fired (%s): %s", dream_type, message[:60])

        except Exception as e:
            logger.warning("Dream event failed: %s", e)

    async def _reset_daily(self) -> None:
        self._proactive_count_today = 0
        self._random_events_today = 0
        self._dream_delivered_today = False
        self._romance_delivered_today = False
        self._user_messaged_today = False
        logger.info("Daily proactive, event, and romance counters reset")
