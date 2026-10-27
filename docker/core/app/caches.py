"""Redis-backed caches: affection state, voice translations.

These caches trade a small amount of freshness for large savings on hot
paths (every request reads affection; a replayed voice line never pays twice).

All operations fail-open: on Redis error the caller falls back to the
uncached source-of-truth. Caches never hide data, only accelerate.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


import hashlib
import json
import logging
import os

import redis.asyncio as aioredis

from .circuit_breakers import get_breaker

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://companion-redis:6379/0")

# TTLs — tuned for freshness vs. hit-rate
AFFECTION_TTL = 15  # 15s — score changes per interaction
VOICE_TRANSLATION_TTL = 30 * 24 * 3600  # 30d — a replayed line never pays twice

_redis: aioredis.Redis | None = None


async def _get() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis_factory: Callable[..., aioredis.Redis] = aioredis.from_url
        _redis = _redis_factory(REDIS_URL, decode_responses=True)
    return _redis


async def close() -> None:
    """Close the module Redis client + its pool (called on app shutdown)."""
    global _redis
    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception:
            pass
        _redis = None


# Bump to retire every cached translation when the translation prompt changes.
VOICE_TRANSLATION_VERSION = "1"


def _voice_key(text: str) -> str:
    h = hashlib.sha256(f"{VOICE_TRANSLATION_VERSION}\n{text}".encode()).hexdigest()
    return f"cache:voice:ja:{h[:32]}"


async def get_affection(user_id: str) -> dict[str, Any] | None:
    """Return cached affection snapshot for user_id, or None."""
    try:
        r = await _get()
        raw = await get_breaker("redis").call(r.get, f"cache:aff:{user_id}")
        if raw is None:
            return None
        parsed: object = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


async def put_affection(user_id: str, snap: dict[str, Any]) -> None:
    """Cache an affection state snapshot."""
    try:
        r = await _get()
        await get_breaker("redis").call(
            r.set,
            f"cache:aff:{user_id}",
            json.dumps(snap, default=str),
            ex=AFFECTION_TTL,
        )
    except Exception:
        pass


async def invalidate_affection(user_id: str) -> None:
    """Clear the affection cache for a user — call on write-path."""
    try:
        r = await _get()
        await get_breaker("redis").call(r.delete, f"cache:aff:{user_id}")
    except Exception:
        pass


async def get_voice_translation(text: str) -> str | None:
    """Return her cached Japanese line for an English line, or None."""
    try:
        r = await _get()
        raw = await get_breaker("redis").call(r.get, _voice_key(text))
        return raw if isinstance(raw, str) and raw else None
    except Exception as e:
        logger.debug("voice translation cache read miss via error: %s", e)
        return None


async def put_voice_translation(text: str, japanese: str) -> None:
    """Cache her Japanese line. Silent-fail on Redis error."""
    try:
        r = await _get()
        await get_breaker("redis").call(
            r.set, _voice_key(text), japanese, ex=VOICE_TRANSLATION_TTL
        )
    except Exception as e:
        logger.debug("voice translation cache write failed: %s", e)
