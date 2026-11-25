"""Redis-backed caches: affection state, voice translations, next-turn recall pins.

These caches trade a small amount of freshness for large savings on hot
paths (every request reads affection; a replayed voice line never pays twice).

All operations fail-open: on Redis error the caller falls back to the
uncached source-of-truth. Caches never hide data, only accelerate.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


import asyncio
import hashlib
import json
import logging
import os
import socket
import threading
import time
from urllib.parse import urlparse

import redis.asyncio as aioredis

from .circuit_breakers import get_breaker

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://companion-redis:6379/0")

# TTLs — tuned for freshness vs. hit-rate
AFFECTION_TTL = 15  # 15s — score changes per interaction
VOICE_TRANSLATION_TTL = 30 * 24 * 3600  # 30d — a replayed line never pays twice
# Next turn only: long enough to survive a restart, then the Postgres order is used.
RECALL_PIN_TTL = 10 * 60
# A hung Redis must not stall the next reply, or the pin job, for a DNS timeout.
_PIN_OP_TIMEOUT_S = 0.3
_PIN_BACKOFF_S = 2.0

_redis: aioredis.Redis | None = None
_pin_redis: aioredis.Redis | None = None
_pin_down_until = 0.0
_PIN_UNAVAILABLE = object()


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
    await _drop_pin_client()


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


def _pin_key(user_id: str) -> str:
    return f"cache:recall-pin:{user_id}"


def _pin_gen_key(user_id: str) -> str:
    return f"cache:recall-pin-gen:{user_id}"


# Generation is a fixed-width decimal string. Redis Lua numbers are doubles,
# so a nanosecond id cannot be compared with tonumber. Equal width sorts in order.
# A newer write replaces an older one; an empty payload clears the texts but
# keeps the generation so a late older write cannot restore them.
_PIN_PUT = """
local current = redis.call('GET', KEYS[1]) or ''
if ARGV[1] < current then
  return 0
end
local ttl = tonumber(ARGV[3])
redis.call('SET', KEYS[1], ARGV[1], 'EX', ttl)
if ARGV[2] == '' then
  redis.call('DEL', KEYS[2])
else
  redis.call('SET', KEYS[2], ARGV[2], 'EX', ttl)
end
return 1
"""

_PIN_DROP = """
local current = redis.call('GET', KEYS[1]) or ''
if current <= ARGV[1] then
  redis.call('DEL', KEYS[2])
  return 1
end
return 0
"""

_PIN_TAKE = """
local raw = redis.call('GET', KEYS[1])
if raw then
  redis.call('DEL', KEYS[1])
end
return raw
"""


def _pin_endpoint() -> tuple[str, int]:
    parsed = urlparse(REDIS_URL)
    return parsed.hostname or "localhost", parsed.port or 6379


async def _wait_until_pin_host_resolves() -> None:
    """Resolve on a daemon thread so a cancelled lookup cannot stall loop shutdown.

    asyncio's default executor is joined at teardown, and a failed DNS lookup of
    the Redis host takes seconds. This thread is not that executor.
    """
    host, port = _pin_endpoint()
    outcome: dict[str, BaseException] = {}

    def lookup() -> None:
        try:
            socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except Exception as exc:
            outcome["err"] = exc

    thread = threading.Thread(target=lookup, name="recall-pin-dns", daemon=True)
    thread.start()
    while thread.is_alive():
        await asyncio.sleep(0.01)
    if "err" in outcome:
        raise outcome["err"]


async def _pin_client() -> aioredis.Redis:
    global _pin_redis
    if _pin_redis is None:
        await _wait_until_pin_host_resolves()
        factory: Callable[..., aioredis.Redis] = aioredis.from_url
        _pin_redis = factory(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=_PIN_OP_TIMEOUT_S,
            socket_timeout=_PIN_OP_TIMEOUT_S,
        )
    return _pin_redis


async def _drop_pin_client() -> None:
    global _pin_redis
    client = _pin_redis
    _pin_redis = None
    if client is None:
        return
    try:
        async with asyncio.timeout(0.2):
            await client.aclose()
    except Exception:
        pass


async def _pin_guard(op: Callable[[], Awaitable[Any]]) -> Any:
    """One short Redis attempt. Failure backs off and never opens the shared breaker.

    Affection and sessions use that breaker. A missed recall pin must not trip it.
    """
    global _pin_down_until
    if time.monotonic() < _pin_down_until:
        return _PIN_UNAVAILABLE
    try:
        async with asyncio.timeout(_PIN_OP_TIMEOUT_S):
            return await op()
    except Exception as exc:
        _pin_down_until = time.monotonic() + _PIN_BACKOFF_S
        logger.debug("recall pin cache unavailable (%s)", type(exc).__name__)
        await _drop_pin_client()
        return _PIN_UNAVAILABLE


async def _pin_eval(script: str, keys: list[str], args: list[str]) -> Any:
    async def run() -> Any:
        client = await _pin_client()
        evaluated = client.eval(script, len(keys), *keys, *args)
        if isinstance(evaluated, str):
            return evaluated
        return await evaluated

    return await _pin_guard(run)


def _decode_pin(raw: object) -> list[str]:
    if not isinstance(raw, str) or not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    texts = [item.strip() for item in parsed if isinstance(item, str) and item.strip()]
    return texts[:8]


async def put_recall_pin(user_id: str, generation: str, texts: list[str]) -> None:
    """Replace this user's next-turn pin when ``generation`` is not older."""
    cleaned = [text for text in texts if isinstance(text, str) and text.strip()]
    payload = json.dumps(cleaned, ensure_ascii=False) if cleaned else ""
    await _pin_eval(
        _PIN_PUT,
        [_pin_gen_key(user_id), _pin_key(user_id)],
        [generation, payload, str(RECALL_PIN_TTL)],
    )


async def drop_recall_pin(user_id: str, generation: str) -> None:
    """Forget the mirrored pin unless a newer generation has already replaced it."""
    await _pin_eval(
        _PIN_DROP,
        [_pin_gen_key(user_id), _pin_key(user_id)],
        [generation],
    )


async def take_recall_pin(user_id: str) -> list[str] | None:
    """Consume the mirrored pin. None means Redis was unreadable."""
    raw = await _pin_eval(_PIN_TAKE, [_pin_key(user_id)], [])
    if raw is _PIN_UNAVAILABLE:
        return None
    return _decode_pin(raw)
