"""Notification handler — health monitor, Redis subscriber, quiet hours."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

import httpx
import redis.asyncio as aioredis
from telegram import Bot

from .config import (
    COMPANION_CORE_URL,
    COMPANION_VOICE_URL,
    HEALTH_CHECK_INTERVAL,
    HEALTH_FAIL_THRESHOLD,
    QUIET_HOUR_END,
    QUIET_HOUR_START,
    REDIS_URL,
)

logger = logging.getLogger(__name__)


class HealthState:
    """Tracks health state with threshold-based alerting."""

    def __init__(self, threshold: int = HEALTH_FAIL_THRESHOLD) -> None:
        self.status: str = "unknown"  # unknown, up, down
        self.consecutive_failures: int = 0
        self.threshold = threshold

    def record_success(self) -> bool:
        """Record a successful check. Returns True if state changed."""
        self.consecutive_failures = 0
        old = self.status
        self.status = "up"
        return old != "up"

    def record_failure(self) -> bool:
        """Record a failed check. Returns True if state changed."""
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            old = self.status
            self.status = "down"
            return old != "down"
        return False


def is_quiet_hour() -> bool:
    """Check if current time is in quiet hours."""
    hour = datetime.now().hour
    return QUIET_HOUR_START <= hour or hour < QUIET_HOUR_END


class NotificationManager:
    """Manages health checks and Redis event subscriptions."""

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._health: dict[str, HealthState] = {
            "core": HealthState(),
            "voice": HealthState(),
        }
        self._http: httpx.AsyncClient | None = None
        self._redis: aioredis.Redis | None = None
        self._queued: list[str] = []

    async def start(self) -> None:
        """Start background tasks."""
        self._http = httpx.AsyncClient(timeout=10.0)
        asyncio.create_task(self._health_loop())
        asyncio.create_task(self._redis_loop())
        asyncio.create_task(self._queue_flush_loop())
        logger.info("Notification manager started")

    async def stop(self) -> None:
        if self._http:
            await self._http.aclose()
        if self._redis:
            await self._redis.aclose()

    async def _notify(self, message: str, bypass_quiet: bool = False) -> None:
        """Send a notification, respecting quiet hours."""
        if is_quiet_hour() and not bypass_quiet:
            self._queued.append(message)
            return
        try:
            await self._bot.send_message(chat_id=self._chat_id, text=message)
        except Exception as e:
            logger.error("Failed to send notification: %s", e)

    # ── Health monitor ───────────────────────────────────────────────────

    async def _check_service(self, name: str, url: str) -> None:
        state = self._health[name]
        try:
            r = await self._http.get(f"{url}/health")
            r.raise_for_status()
            if state.record_success():
                await self._notify(f"[UP] {name} is back up", bypass_quiet=True)
        except Exception:
            if state.record_failure():
                await self._notify(
                    f"[DOWN] {name} unreachable ({state.threshold} consecutive failures)",
                    bypass_quiet=True,
                )

    async def _health_loop(self) -> None:
        while True:
            await self._check_service("core", COMPANION_CORE_URL)
            await self._check_service("voice", COMPANION_VOICE_URL)
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)

    # ── Redis subscriber ─────────────────────────────────────────────────

    async def _redis_loop(self) -> None:
        while True:
            try:
                self._redis = aioredis.from_url(REDIS_URL, decode_responses=True)
                pubsub = self._redis.pubsub()
                await pubsub.subscribe("companion:events")
                logger.info("Subscribed to companion:events")

                async for message in pubsub.listen():
                    if message["type"] != "message":
                        continue
                    try:
                        event = json.loads(message["data"])
                        await self._handle_event(event)
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.warning("Bad Redis event: %s", e)

            except Exception as e:
                logger.warning("Redis error: %s — reconnecting in 10s", e)
                await asyncio.sleep(10)

    async def _handle_event(self, event: dict) -> None:
        etype = event.get("type", "")
        data = event.get("data", "")

        if etype == "proactive":
            await self._notify(f"Klukai: {data}")
        elif etype == "affection_change":
            level = event.get("level", "")
            delta = event.get("delta", 0)
            direction = "+" if delta > 0 else ""
            await self._notify(f"Affection {direction}{delta} -> Level {level}")
        elif etype == "error":
            await self._notify(f"[ERROR] {data}", bypass_quiet=True)
        else:
            await self._notify(f"[{etype}] {data}")

    # ── Queue flush ──────────────────────────────────────────────────────

    async def _queue_flush_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            if not is_quiet_hour() and self._queued:
                batch = self._queued.copy()
                self._queued.clear()
                summary = f"Queued during quiet hours ({len(batch)}):\n\n"
                summary += "\n".join(f"- {m}" for m in batch)
                await self._notify(summary, bypass_quiet=True)
