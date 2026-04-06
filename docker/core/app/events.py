"""Redis event publisher — companion-core -> Telegram bot notifications."""

from __future__ import annotations

import json
import logging
import os

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://aichat-redis:6379/1")
CHANNEL = "companion:events"

_redis: aioredis.Redis | None = None


async def init() -> None:
    global _redis
    _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    logger.info("Event publisher connected to Redis")


async def close() -> None:
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None


async def publish(event_type: str, data: str = "", **kwargs) -> None:
    """Publish an event to companion:events channel."""
    if _redis is None:
        return
    event = {"type": event_type, "data": data, **kwargs}
    try:
        await _redis.publish(CHANNEL, json.dumps(event))
    except Exception as e:
        logger.warning("Failed to publish event: %s", e)
