"""One mission's radio-update task and injury state; no engine instance."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from . import state
from .state import MAJOR_EVENTS

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
        self._task: asyncio.Task[Any] | None = None
        self._callback: Callable[[str], Awaitable[object]] | None = None
        self._affection_level: int = 0

    def start(
        self,
        description: str,
        interval_minutes: int = 30,
        callback: Callable[[str], Awaitable[object]] | None = None,
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
            description[:60],
            interval_minutes,
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
                    if (
                        major_event == "klukai_injured"
                        and "klukai_injured" not in self.active_events
                    ):
                        self.active_events.append("klukai_injured")
                    elif (
                        major_event == "squad_injured"
                        and "squad_injured" not in self.active_events
                    ):
                        self.active_events.append("squad_injured")
                    elif (
                        major_event == "medical_emergency"
                        and "medical_emergency" not in self.active_events
                    ):
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

            # The GPU host is routinely powered off, and there is no cloud
            # fallback: the generator then hands back empty text. Skip the tick
            # rather than write a canned "static" line into his history — the
            # next update fires on schedule regardless.
            if not text:
                logger.info(
                    "Mission update #%d skipped: LLM unavailable", self.update_count
                )
                return

            if self._callback:
                await self._callback(text)
                logger.info(
                    "Mission update #%d delivered (%d min elapsed, event=%s)",
                    self.update_count,
                    elapsed_minutes,
                    major_event or "none",
                )
        except Exception as e:
            logger.error("Mission update delivery failed: %s", e)
