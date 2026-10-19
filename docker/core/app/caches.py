"""Redis-backed caches: embeddings, affection state, voice translations.

These caches trade a small amount of freshness for large savings on hot
paths (every chat turn embeds text; every request reads affection).

All operations fail-open: on Redis error the caller falls back to the
uncached source-of-truth. Caches never hide data, only accelerate.
"""

from __future__ import annotations

from .dependencies import http_request
from collections.abc import Callable
from typing import Any


import hashlib
import json
import logging
import os

import redis.asyncio as aioredis

from .circuit_breakers import get_breaker

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://aichat-redis:6379/1")

# TTLs — tuned for freshness vs. hit-rate
EMBEDDING_TTL = 3600  # 1h — same text embedding is always the same
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


def _text_key(text: str) -> str:
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    return f"cache:embed:{h}"


# Bump to retire every cached translation when the translation prompt changes.
VOICE_TRANSLATION_VERSION = "1"


def _voice_key(text: str) -> str:
    h = hashlib.sha256(f"{VOICE_TRANSLATION_VERSION}\n{text}".encode()).hexdigest()
    return f"cache:voice:ja:{h[:32]}"


async def get_embedding(text: str) -> list[float] | None:
    """Return cached embedding for text, or None."""
    try:
        r = await _get()
        raw = await get_breaker("redis").call(r.get, _text_key(text))
        if raw is None:
            return None
        parsed = json.loads(raw)
        if isinstance(parsed, list) and all(
            isinstance(x, (int, float)) for x in parsed
        ):
            return [float(x) for x in parsed]
        return None
    except Exception as e:
        logger.debug("embedding cache read miss via error: %s", e)
        return None


async def put_embedding(text: str, vector: list[float]) -> None:
    """Cache an embedding. Silent-fail on Redis error."""
    try:
        r = await _get()
        await get_breaker("redis").call(
            r.set, _text_key(text), json.dumps(vector), ex=EMBEDDING_TTL
        )
    except Exception as e:
        logger.debug("embedding cache write failed: %s", e)


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


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors. Returns 0 on error."""
    try:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return float(dot / (na * nb))
    except Exception:
        return 0.0


async def is_duplicate_vector(
    vector: list[float],
    user_id: str,
    collection: str = "companion_episodes",
    threshold: float = 0.95,
    limit: int = 3,
) -> bool:
    """Check Qdrant for a near-identical existing vector.

    Returns True if any of the top-`limit` matches has cosine >= threshold.
    Used by episode-store to skip near-duplicate writes that would bloat
    the vector DB. Always returns False on any Qdrant error (fail-open).
    """
    try:
        import httpx

        qdrant_url = os.environ.get("QDRANT_URL", "http://aichat-vector:6333")
        body = {
            "vector": vector,
            "limit": limit,
            "with_payload": False,
            "filter": {"must": [{"key": "user_id", "match": {"value": user_id}}]},
        }
        async with httpx.AsyncClient(timeout=3.0) as c:
            resp = await http_request(
                "qdrant",
                c,
                "post",
                f"{qdrant_url}/collections/{collection}/points/search",
                json=body,
            )
            if resp.status_code != 200:
                return False
            hits = resp.json().get("result", [])
            for h in hits:
                score = h.get("score", 0.0)
                if score >= threshold:
                    return True
            return False
    except Exception as e:
        logger.debug("dup check failed, treating as non-dup: %s", e)
        return False
