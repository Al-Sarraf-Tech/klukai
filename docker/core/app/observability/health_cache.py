"""TTL-cached subsystem health pings.

`/health` is hit by Docker's healthcheck every 15s by default, plus
Cloudflare uptime probes, plus any operator curl. Pinging PG + Redis +
Qdrant inline on every call burns ~120ms per probe (measured in
Phase 1 perf baseline; see docs/perf-baseline.md). At a 1/sec
aggregate probe rate that's a non-trivial load on the pool.

This module caches each subsystem's last-seen status for `TTL_SECONDS`
and refreshes lazily on read (no background thread — async-friendly,
test-friendly, no shutdown coordination needed).

SLO target (from docs/slos.md): `/health` p99 ≤ 30ms. The cache hit path
should be sub-millisecond; the miss path is the same cost as the pre-cache
implementation.

Usage:

    from .observability.health_cache import get_cached_health

    @app.get("/health")
    async def health():
        return await get_cached_health()

For an uncached deep-check (use sparingly, e.g. for readiness probes):

    from .observability.health_cache import get_fresh_health

    @app.get("/api/health/ready")
    async def ready():
        return await get_fresh_health()
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import AsyncExitStack, asynccontextmanager
from .. import __version__
from dataclasses import dataclass
from typing import Any

import httpx
import redis.asyncio as aioredis
from ..dependencies import http_request, json_object
from ..circuit_breakers import get_breaker
from collections.abc import Awaitable, Callable
from typing import cast
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

# ── Tunables (environment-overridable) ──────────────────────────────────────
TTL_SECONDS = float(os.getenv("HEALTH_CACHE_TTL_SECONDS", "5.0"))
PING_TIMEOUT_SECONDS = float(os.getenv("HEALTH_CACHE_PING_TIMEOUT", "2.0"))


@dataclass
class _CacheEntry:
    """Single subsystem result + age."""

    status: dict[str, Any]
    captured_at: float

    def is_fresh(self, now: float | None = None) -> bool:
        return (now or time.monotonic()) - self.captured_at < TTL_SECONDS


# Module-level cache. Single-process, so a plain dict is enough.
# Each entry is keyed by subsystem name → _CacheEntry.
_cache: dict[str, _CacheEntry] = {}

# Single in-flight refresh per subsystem prevents thundering-herd refresh
# when many concurrent requests find the cache stale.
_refresh_locks: dict[str, asyncio.Lock] = {
    "database": asyncio.Lock(),
    "redis": asyncio.Lock(),
    "qdrant": asyncio.Lock(),
}


@dataclass
class _ProbeClients:
    http: httpx.AsyncClient
    redis: aioredis.Redis


_clients: _ProbeClients | None = None
_running = False


@asynccontextmanager
async def running() -> AsyncIterator[None]:
    """Own pooled probe transports for one application lifespan.

    Each readiness request still pings every dependency. Only transports are
    reused: rebuilding HTTP TLS roots and Redis metadata per ping is needless
    CPU work. TLS verification and environment trust roots retain their defaults.
    Standalone callers without a lifespan retain temporary, promptly closed clients.
    """
    global _clients, _running
    if _running:
        raise RuntimeError("Health probe clients already have a lifespan owner")
    _running = True
    try:
        from ..memory import REDIS_URL

        async with AsyncExitStack() as cleanup:
            client = httpx.AsyncClient(
                timeout=PING_TIMEOUT_SECONDS,
                limits=httpx.Limits(max_connections=3, max_keepalive_connections=3),
            )
            cleanup.push_async_callback(client.aclose)
            factory: Callable[..., aioredis.Redis] = aioredis.from_url
            cache = factory(
                REDIS_URL,
                decode_responses=True,
                max_connections=3,
                socket_timeout=PING_TIMEOUT_SECONDS,
                socket_connect_timeout=PING_TIMEOUT_SECONDS,
            )
            cleanup.push_async_callback(cache.aclose)
            _clients = _ProbeClients(client, cache)
            try:
                yield
            finally:
                _clients = None
                clear_cache()
    finally:
        _running = False


# ── Per-subsystem fresh-check implementations ───────────────────────────────


async def _check_database_fresh() -> dict[str, Any]:
    from ..db import check_health as db_health

    try:
        return await asyncio.wait_for(db_health(), timeout=PING_TIMEOUT_SECONDS)
    except TimeoutError:
        # Under pool exhaustion the SELECT 1 waits for a connection with no
        # bound of its own. Left alone, /health would hang past the compose
        # healthcheck (timeout 5s x 3 retries) and autoheal would restart
        # core mid-chat. Same shape db.check_health returns on failure.
        logger.warning(
            "database health probe timed out after %.1fs", PING_TIMEOUT_SECONDS
        )
        return {"status": "error", "detail": "probe timeout"}


async def _check_redis_fresh() -> dict[str, Any]:
    try:
        from ..memory import REDIS_URL

        if _clients is not None:
            await _ping_redis(_clients.redis)
            return {"status": "ok"}
        factory: Callable[..., aioredis.Redis] = aioredis.from_url
        r = factory(REDIS_URL, decode_responses=True)
        try:
            await _ping_redis(r)
            return {"status": "ok"}
        finally:
            await r.aclose()
    except Exception as e:
        logger.warning("redis health probe failed: %s", e)
        return {"status": "down"}


async def _ping_redis(client: aioredis.Redis) -> None:
    await asyncio.wait_for(
        get_breaker("redis").call(cast(Callable[[], Awaitable[bool]], client.ping)),
        timeout=PING_TIMEOUT_SECONDS,
    )


async def _check_qdrant_fresh() -> dict[str, Any]:
    try:
        from ..memory import QDRANT_URL

        if _clients is not None:
            resp = await http_request(
                "qdrant", _clients.http, "get", f"{QDRANT_URL}/healthz"
            )
            return {"status": "ok"} if resp.status_code == 200 else {"status": "down"}
        async with httpx.AsyncClient(timeout=PING_TIMEOUT_SECONDS) as client:
            resp = await http_request("qdrant", client, "get", f"{QDRANT_URL}/healthz")
            return {"status": "ok"} if resp.status_code == 200 else {"status": "down"}
    except Exception as e:
        logger.warning("qdrant health probe failed: %s", e)
        return {"status": "down"}


_FRESH_CHECKS = {
    "database": "_check_database_fresh",
    "redis": "_check_redis_fresh",
    "qdrant": "_check_qdrant_fresh",
}


async def _get_or_refresh(subsystem: str) -> dict[str, Any]:
    """Return cached value if fresh; else refresh under a per-subsystem lock."""
    now = time.monotonic()
    entry = _cache.get(subsystem)
    if entry is not None and entry.is_fresh(now):
        return entry.status

    # Stale or absent. Acquire per-subsystem lock; only one refresher runs.
    lock = _refresh_locks[subsystem]
    async with lock:
        # Double-check: another coroutine may have refreshed while we waited.
        entry = _cache.get(subsystem)
        if entry is not None and entry.is_fresh():
            return entry.status

        # Dynamic lookup so test-time `patch.object(health_cache, "_check_X_fresh", ...)`
        # actually swaps the function being called.
        import sys

        module = sys.modules[__name__]
        fresh_fn = getattr(module, _FRESH_CHECKS[subsystem])
        fresh = json_object(await fresh_fn())
        _cache[subsystem] = _CacheEntry(status=fresh, captured_at=time.monotonic())
        return fresh


# ── Public API ──────────────────────────────────────────────────────────────


async def get_cached_health() -> dict[str, Any]:
    """Return rolled-up health using the cache. Sub-millisecond on cache hit."""
    db, redis, qdrant = await asyncio.gather(
        _get_or_refresh("database"),
        _get_or_refresh("redis"),
        _get_or_refresh("qdrant"),
    )

    db_ok = db.get("status") == "ok"
    redis_ok = redis.get("status") == "ok"
    qdrant_ok = qdrant.get("status") == "ok"

    if db_ok and redis_ok and qdrant_ok:
        status = "ok"
    elif db_ok:
        status = "degraded"
    else:
        status = "unhealthy"

    return {
        "status": status,
        "service": "companion-core",
        "version": __version__,
        "database": db,
        "redis": redis.get("status", "unknown"),
        "qdrant": qdrant.get("status", "unknown"),
        "cache": {
            "ttl_seconds": TTL_SECONDS,
            "entries": list(_cache.keys()),
        },
    }


async def get_fresh_health() -> dict[str, Any]:
    """Force a cache refresh and return the rolled-up health.

    Use sparingly — this incurs the full backend round-trip cost. Suitable
    for K8s-style readiness probes or post-deploy smoke checks.
    """
    # Invalidate then read; the underlying _get_or_refresh path will refresh.
    _cache.clear()
    result = await get_cached_health()
    # Sessions cannot be admitted without Redis, even though cached /health
    # keeps its historical optional-subsystem degradation description.
    if result.get("redis") != "ok":
        result["status"] = "unhealthy"
    return result


def get_live_health() -> dict[str, Any]:
    """Liveness probe — process-level only. No backend pings.

    Returns 200 unless the process itself is broken. Use for K8s-style
    liveness probes that should NOT cause a restart when a backend is down
    (that's a readiness concern).
    """
    return {
        "status": "ok",
        "service": "companion-core",
        "version": __version__,
    }


def clear_cache() -> None:
    """Test helper: drop all cached entries so the next read forces a refresh.

    Also recreates the per-subsystem refresh locks: asyncio.Lock binds to the
    event loop that first acquires it, and pytest-asyncio gives each test a
    fresh loop — a lock carried over from a previous test's loop makes the
    double-check-lock path nondeterministic (seen as a flaky
    test_concurrent_stale_callers_refresh_once under mutmut's runner).
    """
    _cache.clear()
    for subsystem in _refresh_locks:
        _refresh_locks[subsystem] = asyncio.Lock()
