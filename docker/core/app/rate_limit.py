"""Redis-backed per-user per-endpoint rate limiting.

Each (user_id, bucket) combo gets a token counter in Redis with a sliding
window (INCR + EXPIRE on first hit). Limits are defined per-bucket.

Fail-open: if Redis is unavailable we allow the request and log a warning.
Failing closed on rate limiter would be worse than letting the app work
slightly degraded.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://aichat-redis:6379/1")

_redis: aioredis.Redis | None = None


async def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


@dataclass(frozen=True)
class Limit:
    """A single rate limit: N requests per window_seconds."""
    requests: int
    window_seconds: int

    @property
    def key_prefix(self) -> str:
        return f"ratelimit:{self.requests}/{self.window_seconds}"


# Default limits per bucket. Tune per endpoint.
LIMITS: dict[str, Limit] = {
    "default":    Limit(requests=60,  window_seconds=60),   # 60/min fallback
    "login":      Limit(requests=10,  window_seconds=300),  # 10 logins / 5 min
    "export":     Limit(requests=3,   window_seconds=3600), # 3 exports / hour
    "tts":        Limit(requests=120, window_seconds=60),   # 2/sec sustained
    "stt":        Limit(requests=30,  window_seconds=60),
    "image_gen":  Limit(requests=20,  window_seconds=3600), # expensive
    "gift":       Limit(requests=30,  window_seconds=3600),
    "mission":    Limit(requests=15,  window_seconds=3600),
    "search":     Limit(requests=60,  window_seconds=60),
    "stats":      Limit(requests=30,  window_seconds=60),
}


class RateLimitExceeded(Exception):
    """Raised when a request exceeds its bucket's limit."""

    def __init__(self, bucket: str, retry_after: int):
        self.bucket = bucket
        self.retry_after = retry_after
        super().__init__(f"Rate limit exceeded for bucket '{bucket}'; retry in {retry_after}s")


async def check_and_consume(user_id: str, bucket: str) -> tuple[int, int]:
    """Check the limit for (user_id, bucket). Consume one token.

    Returns (remaining, retry_after_seconds). Raises RateLimitExceeded if
    the bucket is full.

    Fail-open on Redis errors: logs a warning and returns (999, 0).
    """
    limit = LIMITS.get(bucket, LIMITS["default"])
    key = f"{limit.key_prefix}:{user_id}:{bucket}"
    try:
        r = await _get_redis()
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, limit.window_seconds)
        ttl = await r.ttl(key)
        if count > limit.requests:
            raise RateLimitExceeded(bucket=bucket, retry_after=max(ttl, 1))
        return (limit.requests - count, max(ttl, 0))
    except RateLimitExceeded:
        raise
    except Exception as e:
        logger.warning("Rate limiter unavailable for bucket=%s user=%s: %s",
                       bucket, user_id[:16], e)
        return (999, 0)


async def reset(user_id: str, bucket: str) -> None:
    """Clear a user's bucket counter. Admin/testing only."""
    limit = LIMITS.get(bucket, LIMITS["default"])
    key = f"{limit.key_prefix}:{user_id}:{bucket}"
    try:
        r = await _get_redis()
        await r.delete(key)
    except Exception as e:
        logger.debug("Rate limit reset failed: %s", e)
