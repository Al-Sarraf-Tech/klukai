"""Redis-backed per-user per-endpoint rate limiting.

Each (user_id, bucket) combo gets a token counter in Redis with a sliding
window (INCR + EXPIRE on first hit). Limits are defined per-bucket.

Fail-open: if Redis is unavailable we allow the request and log a warning.
Failing closed on rate limiter would be worse than letting the app work
slightly degraded.
"""

from __future__ import annotations
from collections.abc import Awaitable, Callable
from typing import Any, cast


import logging
import os
from dataclasses import dataclass

import redis.asyncio as aioredis

from .circuit_breakers import get_breaker

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://aichat-redis:6379/1")

_redis: aioredis.Redis | None = None


async def _get_redis() -> aioredis.Redis:
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
    "default": Limit(requests=60, window_seconds=60),  # 60/min fallback
    "login": Limit(requests=10, window_seconds=300),  # 10 logins / 5 min
    "export": Limit(requests=3, window_seconds=3600),  # 3 exports / hour
    "tts": Limit(requests=120, window_seconds=60),  # 2/sec sustained
    "voice_jobs": Limit(
        requests=240, window_seconds=60
    ),  # two active polls + cancellation
    "stt": Limit(requests=30, window_seconds=60),
    "image_gen": Limit(requests=20, window_seconds=3600),  # expensive
    "gift": Limit(requests=30, window_seconds=3600),
    "mission": Limit(requests=15, window_seconds=3600),
    "search": Limit(requests=60, window_seconds=60),
    "stats": Limit(requests=30, window_seconds=60),
    "portrait_refresh": Limit(
        requests=1, window_seconds=600
    ),  # re-renders 6 GPU images
}


class RateLimitExceeded(Exception):
    """Raised when a request exceeds its bucket's limit."""

    def __init__(self, bucket: str, retry_after: int) -> None:
        self.bucket = bucket
        self.retry_after = retry_after
        super().__init__(
            f"Rate limit exceeded for bucket '{bucket}'; retry in {retry_after}s"
        )


@dataclass(frozen=True)
class Consumption:
    """The token one admitted request took: the only thing refund() accepts.

    ``window_expires_at`` is the window's EXPIRETIME (unix seconds), which is
    fixed for the life of a window, so a refund can never land in a later
    window that other requests are spending.
    """

    key: str
    window_expires_at: int


# Give one token back, atomically, only to the window it came from and never
# below zero. EXPIRETIME is -2 for a missing key and -1 without a TTL, so
# neither can match a receipt.
_REFUND_SCRIPT = """
if redis.call('EXPIRETIME', KEYS[1]) ~= tonumber(ARGV[1]) then return 0 end
local count = tonumber(redis.call('GET', KEYS[1]) or '0')
if count == nil or count <= 0 then return 0 end
redis.call('DECR', KEYS[1])
return 1
"""


async def check_and_consume(user_id: str, bucket: str) -> tuple[int, int]:
    """Check the limit for (user_id, bucket). Consume one token.

    Returns (remaining, retry_after_seconds). Raises RateLimitExceeded if
    the bucket is full.

    Fail-open on Redis errors: logs a warning and returns (999, 0).
    """
    remaining, retry_after, _ = await consume(user_id, bucket)
    return remaining, retry_after


async def consume(user_id: str, bucket: str) -> tuple[int, int, Consumption | None]:
    """check_and_consume plus a receipt for refund().

    The receipt is None when no token was verifiably taken: the limiter
    failed open, or the window's expiry could not be read.
    """
    limit = LIMITS.get(bucket, LIMITS["default"])
    key = f"{limit.key_prefix}:{user_id}:{bucket}"
    receipt: Consumption | None = None
    try:
        r = await _get_redis()
        async with get_breaker("redis").protect():
            count = await r.incr(key)
            if count == 1:
                await r.expire(key, limit.window_seconds)
            ttl = await r.ttl(key)
            if ttl < 0:
                # -1: the key has no TTL; the EXPIRE after the first INCR never
                # landed, so the counter would outlive its window and the
                # bucket would stay locked forever (a 429 whose retry_after
                # never clears). Re-arm it and judge this request against a
                # full window. (-2: the key vanished in between; EXPIRE is a
                # no-op.)
                await r.expire(key, limit.window_seconds)
                ttl = limit.window_seconds
            try:
                # redis-py types its asyncio commands as the sync return value.
                expires_at = int(await cast(Awaitable[Any], r.expiretime(key)))
            except Exception:
                expires_at = 0  # admission stands; this token just can't be refunded
            if expires_at > 0:
                receipt = Consumption(key, expires_at)
        if count > limit.requests:
            raise RateLimitExceeded(bucket=bucket, retry_after=max(ttl, 1))
        return (limit.requests - count, max(ttl, 0), receipt)
    except RateLimitExceeded:
        raise
    except Exception as e:
        logger.warning(
            "Rate limiter unavailable for bucket=%s user=%s: %s",
            bucket,
            user_id[:16],
            e,
        )
        return (999, 0, None)


async def refund(receipt: Consumption | None) -> bool:
    """Give back the token consume() took for one request.

    For requests the limiter admitted but the handler then turned away
    without doing the work the limit guards: a 409 "GPU busy" on the
    1-per-10-minutes portrait bucket must not burn its only token. Only a
    real receipt refunds, only into the window it was taken from, and never
    below zero, in one atomic script. Fail-soft: a Redis error only logs.
    Returns True when a token went back.
    """
    if receipt is None:
        return False
    try:
        r = await _get_redis()
        async with get_breaker("redis").protect():
            done = await cast(
                Awaitable[Any],
                r.eval(_REFUND_SCRIPT, 1, receipt.key, str(receipt.window_expires_at)),
            )
        return bool(done == 1)
    except Exception as e:
        logger.debug("Rate limit refund failed for %s: %s", receipt.key, e)
        return False


async def reset(user_id: str, bucket: str) -> None:
    """Clear a user's bucket counter. Admin/testing only."""
    limit = LIMITS.get(bucket, LIMITS["default"])
    key = f"{limit.key_prefix}:{user_id}:{bucket}"
    try:
        r = await _get_redis()
        await get_breaker("redis").call(r.delete, key)
    except Exception as e:
        logger.debug("Rate limit reset failed: %s", e)
