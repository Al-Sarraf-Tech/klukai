"""Redis event publisher — companion-core -> Telegram bot notifications."""

from __future__ import annotations
from collections.abc import Callable
from typing import Any


import json
import logging
import os

import redis.asyncio as aioredis

from .circuit_breakers import get_breaker

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://companion-redis:6379/0")
CHANNEL = "companion:events"

_redis: aioredis.Redis | None = None


async def init() -> None:
    global _redis
    _redis_factory: Callable[..., aioredis.Redis] = aioredis.from_url
    _redis = _redis_factory(REDIS_URL, decode_responses=True)
    logger.info("Event publisher connected to Redis")


async def close() -> None:
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None


async def publish(event_type: str, data: str = "", **kwargs: Any) -> int:
    """Publish an event to companion:events channel.

    Returns the number of subscribers Redis delivered to (0 when Redis is
    not configured or the publish failed). Pub/sub has no queue: an event
    published while nobody is subscribed is gone. The count is only a Redis
    receipt, never durable broker acceptance; Her POV recovers from its DB row.
    """
    if _redis is None:
        return 0
    event = {"type": event_type, "data": data, **kwargs}
    try:
        received = await get_breaker("redis").call(
            _redis.publish, CHANNEL, json.dumps(event)
        )
    except Exception as e:
        logger.warning("Failed to publish event: %s", e)
        return 0
    return int(received or 0)
