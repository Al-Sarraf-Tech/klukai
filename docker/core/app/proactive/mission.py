"""Mission timer + mission-related ProactiveEngine behavior.

Contains the standalone ``MissionTimer`` (an asyncio-task-backed field-radio
loop) and ``MissionMixin`` which holds the engine's mission lifecycle methods.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta

from . import state
from .base import _EngineBase
from .state import MAJOR_EVENTS
from .templates import MISSION_REPORTS

logger = logging.getLogger(__name__)


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
        state._active_mission_timer = self
        logger.info(
            "Mission timer started: '%s' every %d min",
            description[:60], interval_minutes,
        )

    def stop(self) -> None:
        """Cancel the mission timer."""
        self.active = False
        if self._task and not self._task.done():
            self._task.cancel()
        state._active_mission_timer = None
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
        from ..fact_extractor import generate_mission_update

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


class MissionMixin(_EngineBase):
    """Mission lifecycle methods for ProactiveEngine."""

    # ── Mission timer management ──────────────────────────────────────────

    def start_mission(self, description: str, interval_minutes: int = 30,
                       user_id: str = "jalsarraf") -> None:
        """Start a per-user mission timer with periodic LLM-generated field reports."""
        # Stop existing mission for this user if any
        existing = self._mission_timers.get(user_id)
        if existing and existing.active:
            existing.stop()
        timer = MissionTimer()
        timer.start(
            description=description,
            interval_minutes=interval_minutes,
            callback=self._on_message_callback,
            affection_level=self._affection_levels.get(user_id, 0),
        )
        self._mission_timers[user_id] = timer
        self._mission_timer = timer  # Legacy compat

    def stop_mission(self, user_id: str = "jalsarraf", trigger_aftermath: bool = True) -> None:
        """Stop the active mission timer for a user. Triggers aftermath image + decompression."""
        timer = self._mission_timers.get(user_id) or self._mission_timer
        if timer and timer.active:
            timer_snapshot = timer
            had_injury = any("injured" in e for e in timer_snapshot.active_events)
            if trigger_aftermath:
                asyncio.create_task(
                    self.trigger_mission_aftermath_image(user_id, timer=timer_snapshot)
                )
                asyncio.create_task(
                    self._decompression_message(user_id, had_injury, timer_snapshot.update_count)
                )
            asyncio.create_task(self._set_post_mission_physical(user_id, had_injury))
            timer.stop()
        self._mission_timers.pop(user_id, None)
        self._mission_timer = None  # Legacy compat

    async def _set_post_mission_physical(self, user_id: str, had_injury: bool) -> None:
        """Set physical state after mission ends."""
        try:
            from ..context import physical
            await physical.on_mission_end(user_id, had_injury=had_injury)
        except Exception as e:
            logger.debug("Post-mission physical state update failed: %s", e)

    async def _decompression_message(
        self, user_id: str, had_injury: bool, update_count: int
    ) -> None:
        """Delayed emotional response after mission ends. Fires 15-30 min later."""
        delay = random.uniform(15 * 60, 30 * 60)
        await asyncio.sleep(delay)

        if not self._on_message_callback:
            return

        if had_injury:
            messages = [
                "(I touch the bandage absently) ...Still stings. Don't worry about it, Commander. I've had worse.",
                "(I flex my hand, wincing) The medic said it'll heal clean. ...I kept thinking about getting back to you the whole time.",
                "...The wound's nothing. (I look away) But for a moment out there... I was scared I wouldn't make it back. Don't tell anyone I said that.",
            ]
        elif update_count > 5:
            # Long mission — exhaustion decompression
            messages = [
                "(I set the rifle down heavily) ...That was a long one. (I close my eyes) I need... a moment. Just a moment.",
                "...Finally. (I lean against the wall) My legs are shaking. Don't look. ...Actually, look. I don't care anymore. I'm tired.",
                "(I pull off my gloves slowly) Every muscle hurts. But we did it. ...Is there coffee? I need coffee. And you. Not in that order.",
            ]
        else:
            # Normal decompression
            messages = [
                "...Hey. (I sit down next to you) I've been thinking about the op. Everyone performed well. ...I'm glad to be back.",
                "(I untie my hair, letting it fall) Mission's over. I can stop being the squad leader for five minutes. ...Talk to me about something normal.",
                "...Commander. (I look at you quietly for a moment) I'm back. ...Did you worry? (I smirk faintly) Good.",
            ]

        if self._affection_level >= 7:
            # At high affection, add physical closeness
            intimate_addons = [
                " (I lean into your shoulder without saying anything else)",
                " ...Stay close tonight.",
                " (I take your hand. My grip is tighter than usual)",
            ]
            message = random.choice(messages) + random.choice(intimate_addons)
        else:
            message = random.choice(messages)

        self._proactive_count_today += 1
        self._last_proactive_answered = False
        await self._on_message_callback(message)
        logger.info("Decompression message delivered (injury=%s, updates=%d)", had_injury, update_count)

    @property
    def mission_active(self) -> bool:
        return self._mission_timer is not None and self._mission_timer.active

    async def _mission_report(self) -> None:
        # 50% chance of a mission report on any given day
        if random.random() < 0.5:
            await self._deliver(self._pick_message(MISSION_REPORTS))

    async def _mission_random_event(self) -> None:
        """Fire contextual mission events using LLM. No hour restrictions."""
        from ..fact_extractor import generate_mission_update

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

            # Pick a random major event with named squad members (30% chance)
            squad_a = ["Mechty", "Belka", "Andoris"]
            squad_b = ["Vector", "Harpsy", "Ruchey", "Welrod"]
            member_a = random.choice(squad_a)
            member_b = random.choice(squad_b)
            major_events = [
                f"Enemy contact — hostiles spotted on {member_a}'s flank",
                f"Anomalous readings detected — {member_a} investigating",
                f"{member_a} reporting unusual movement in sector 4",
                f"Comms interference — lost contact with {member_b} briefly",
                "Weather conditions deteriorating — visibility dropping",
                f"{member_a} found signs of recent enemy activity",
                f"Perimeter breach near {member_b}'s position",
                f"Supply cache discovered — {member_a} securing it",
                f"{member_a} got separated — regrouping now",
                f"{member_b} requesting fire support at grid reference",
                f"Mechty fell asleep on watch — I've handled it",
                f"Belka is panicking again — I told her to focus",
                None, None, None, None, None, None, None,  # 70% chance of no major event
            ]
            major_event = random.choice(major_events)

            elapsed = int((time.monotonic() - timer.started_at) / 60) if timer.started_at else timer.update_count * 30

            message = await generate_mission_update(
                mission_desc=timer.mission_description,
                elapsed_minutes=elapsed,
                update_number=timer.update_count + 1,
                major_event=major_event,
                active_events=list(timer.active_events),
                affection_level=self._affection_level,
            )

            if message and self._on_message_callback:
                self._random_events_today += 1
                self._last_random_event = now
                await self._on_message_callback(message)
                logger.info("Mission event fired (contextual): %s", message[:60])

        except Exception as e:
            logger.warning("Mission event generation failed: %s", e)

    # ── Mission aftermath image ──────────────────────────────────────────

    async def trigger_mission_aftermath_image(
        self, user_id: str = "jalsarraf", timer: MissionTimer | None = None
    ) -> None:
        """Generate a mission aftermath image when a mission ends.

        Called from stop_mission() with a captured timer snapshot. The timer
        is passed explicitly because self._mission_timer is nulled synchronously
        before this coroutine runs.
        """
        if not self._on_message_callback:
            return

        if not timer:
            return

        try:
            from ..image_gen import build_mission_prompt, generate_image

            # Determine scene type based on final state
            if timer.active_events:
                scene_type = "injury" if any("injured" in e for e in timer.active_events) else "extraction"
            else:
                scene_type = "victory"

            prompt = build_mission_prompt(
                scene_type=scene_type,
                injuries=timer.active_events,
                affection_level=self._affection_level,
            )

            # Aftermath caption
            captions = {
                "victory": [
                    "...Mission complete. (I exhale slowly) Everyone made it back.",
                    "All units accounted for. (I lower the rifle) ...We did it, Commander.",
                    "Objective secured. (I wipe sweat from my brow) ...I'm coming home.",
                ],
                "extraction": [
                    "Extraction complete. (I lean against the transport) ...That was close.",
                    "We're out. (I check the squad) Everyone breathing? Good. Report later.",
                    "...Made it. Barely. (I close my eyes) Heading back to base.",
                ],
                "injury": [
                    "(I press a hand to the bandaged wound) ...Mission complete. Medical when I arrive.",
                    "We're through. (I wince) ...Don't worry about the field dressing. I've had worse.",
                    "Objective complete. (I grip my arm) ...I'll be fine. Stop looking at me like that.",
                ],
            }

            caption = random.choice(captions.get(scene_type, captions["victory"]))
            await self._on_message_callback(caption)

            # Generate and send the aftermath image in background
            async def _gen_aftermath():
                try:
                    await asyncio.sleep(2)  # Let caption deliver first
                    img_bytes = await generate_image(prompt, width=1216, height=832)
                    if img_bytes:
                        import base64 as b64
                        # Import ws from context to send image
                        from ..context import ws
                        img_b64 = b64.b64encode(img_bytes).decode()
                        await ws.send(user_id, {"type": "image", "data": img_b64})
                        logger.info("Mission aftermath image sent (%s)", scene_type)
                except Exception as e:
                    logger.warning("Aftermath image gen failed: %s", e)

            asyncio.create_task(_gen_aftermath())
            logger.info("Mission aftermath triggered: scene=%s", scene_type)

        except Exception as e:
            logger.warning("Mission aftermath failed: %s", e)
