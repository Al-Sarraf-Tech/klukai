"""One mission's radio-update task and injury state; no engine instance."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from . import state
from .state import MAJOR_EVENTS

logger = logging.getLogger(__name__)

# A mission she starts on her own (a battle_ready mood, see
# background._apply_extracted_state) ends by itself after this long. Nobody
# asked for it, so nobody will say "stand down": without a ceiling it ticked
# every ~30 minutes, through the night, until the cancel phrase or a restart.
# A mission he asked for ("keep me posted every 30 min") has no ceiling.
AUTO_MISSION_MAX_MINUTES = 180


def ceiling_minutes_left(
    started_at: str | None, max_minutes: int | None, now: datetime | None = None
) -> float | None:
    """Minutes left before a session mission's ceiling (<= 0 once it passed).

    ``started_at`` is the session's isoformat stamp from the container clock.
    None means the mission has no ceiling. A ceilinged mission whose stamp is
    missing or unparseable keeps its whole allowance.
    """
    if max_minutes is None:
        return None
    if not started_at:
        return float(max_minutes)
    try:
        started = datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        return float(max_minutes)
    if started.tzinfo is not None:
        started = started.astimezone().replace(tzinfo=None)
    elapsed = ((now or datetime.now()) - started).total_seconds() / 60
    return max_minutes - elapsed


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
        self.max_duration_minutes: int | None = None
        # The session's mission_started_at for this mission: its identity.
        self.session_stamp: str | None = None
        self._task: asyncio.Task[Any] | None = None
        self._callback: Callable[[str], Awaitable[object]] | None = None
        self._on_expire: Callable[[], object] | None = None
        self._affection_level: int = 0

    def start(
        self,
        description: str,
        interval_minutes: int = 30,
        callback: Callable[[str], Awaitable[object]] | None = None,
        affection_level: int = 0,
        max_duration_minutes: int | None = None,
        on_expire: Callable[[], object] | None = None,
    ) -> None:
        """Begin the mission timer as an asyncio.Task.

        ``max_duration_minutes`` sets a ceiling: the first tick at or past it
        stands the mission down instead of firing, and ``on_expire`` (sync) is
        called so the engine can close it out like a cancelled mission.
        """
        self.mission_description = description
        self.base_interval_minutes = max(5, interval_minutes)  # Floor at 5 min
        self.started_at = time.monotonic()
        self.last_update = self.started_at
        self.update_count = 0
        self.active_events = []
        self.active = True
        self.max_duration_minutes = max_duration_minutes
        self._callback = callback
        self._on_expire = on_expire
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
        # The ceiling closes a mission out from inside its own tick loop;
        # that task is already ending and must not cancel itself.
        if (
            self._task
            and not self._task.done()
            and self._task is not asyncio.current_task()
        ):
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

                if self._past_ceiling():
                    logger.info(
                        "Mission '%s' reached its %d-minute ceiling after %d updates — standing down",
                        self.mission_description[:60],
                        self.max_duration_minutes,
                        self.update_count,
                    )
                    # The engine's close-out (aftermath, decompression,
                    # physical state) only runs for a mission that is still
                    # active, so it is called before the timer stops itself.
                    if self._on_expire:
                        self._on_expire()
                    self.active = False
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

    def _past_ceiling(self) -> bool:
        """True once a ceilinged mission has run for its full allowance."""
        if self.max_duration_minutes is None:
            return False
        return (time.monotonic() - self.started_at) >= self.max_duration_minutes * 60

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
