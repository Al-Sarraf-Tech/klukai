"""Redis-backed caches: embeddings and affection state.

These caches trade a small amount of freshness for large savings on hot
paths (every chat turn embeds text; every request reads affection).

All operations fail-open: on Redis error the caller falls back to the
uncached source-of-truth. Caches never hide data, only accelerate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://aichat-redis:6379/1")

# TTLs — tuned for freshness vs. hit-rate
EMBEDDING_TTL = 3600           # 1h — same text embedding is always the same
AFFECTION_TTL = 15             # 15s — score changes per interaction

_redis: aioredis.Redis | None = None


async def _get() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


def _text_key(text: str) -> str:
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    return f"cache:embed:{h}"


async def get_embedding(text: str) -> list[float] | None:
    """Return cached embedding for text, or None."""
    try:
        r = await _get()
        raw = await r.get(_text_key(text))
        if raw is None:
            return None
        parsed = json.loads(raw)
        if isinstance(parsed, list) and all(isinstance(x, (int, float)) for x in parsed):
            return [float(x) for x in parsed]
        return None
    except Exception as e:
        logger.debug("embedding cache read miss via error: %s", e)
        return None


async def put_embedding(text: str, vector: list[float]) -> None:
    """Cache an embedding. Silent-fail on Redis error."""
    try:
        r = await _get()
        await r.set(_text_key(text), json.dumps(vector), ex=EMBEDDING_TTL)
    except Exception as e:
        logger.debug("embedding cache write failed: %s", e)


async def get_affection(user_id: str) -> dict | None:
    """Return cached affection snapshot for user_id, or None."""
    try:
        r = await _get()
        raw = await r.get(f"cache:aff:{user_id}")
        if raw is None:
            return None
        return json.loads(raw)
    except Exception:
        return None


async def put_affection(user_id: str, snap: dict) -> None:
    """Cache an affection state snapshot."""
    try:
        r = await _get()
        await r.set(f"cache:aff:{user_id}", json.dumps(snap, default=str),
                    ex=AFFECTION_TTL)
    except Exception:
        pass


async def invalidate_affection(user_id: str) -> None:
    """Clear the affection cache for a user — call on write-path."""
    try:
        r = await _get()
        await r.delete(f"cache:aff:{user_id}")
    except Exception:
        pass


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors. Returns 0 on error."""
    try:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)
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
            resp = await c.post(f"{qdrant_url}/collections/{collection}/points/search",
                                json=body)
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
