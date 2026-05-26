"""Behavioral coverage for app.observability.health_cache — TTL health cache.

The clock (time.monotonic) is patched to a controllable fake so cache
hit/miss/expiry is fully deterministic — no sleeps. The per-subsystem
fresh-check coroutines are patched on the module object (the code looks them
up dynamically via sys.modules, so patch.object swaps the real call). Each
test clears the module cache first for isolation.

Behaviors asserted:
- _CacheEntry.is_fresh respects TTL boundaries.
- _get_or_refresh: cold miss runs the check; hit within TTL reuses; expiry
  re-runs; concurrent stale callers refresh exactly once (double-check lock).
- get_cached_health rolls up ok / degraded / unhealthy correctly.
- get_fresh_health invalidates then refreshes.
- get_live_health is a pure process-level probe (no backend calls).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import app.memory up front so that (a) the memory↔memory_exchange import cycle
# settles and (b) patch targets like app.memory.REDIS_URL resolve.
import app.memory  # noqa: F401,E402
from app.observability import health_cache as hc  # noqa: E402


class _FakeClock:
    """Monotonic clock we advance by hand."""

    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


@pytest.fixture(autouse=True)
def _isolate_cache():
    """Each test starts with an empty cache."""
    hc.clear_cache()
    yield
    hc.clear_cache()


# ── _CacheEntry.is_fresh ─────────────────────────────────────────────────────

class TestCacheEntry:
    def test_fresh_within_ttl(self):
        entry = hc._CacheEntry(status={"status": "ok"}, captured_at=100.0)
        # 100 + 4.9 < 100 + TTL(5) → fresh
        assert entry.is_fresh(now=104.9) is True

    def test_stale_at_ttl_boundary(self):
        entry = hc._CacheEntry(status={"status": "ok"}, captured_at=100.0)
        # Exactly TTL seconds later is NOT < TTL → stale.
        assert entry.is_fresh(now=100.0 + hc.TTL_SECONDS) is False

    def test_stale_after_ttl(self):
        entry = hc._CacheEntry(status={"status": "ok"}, captured_at=100.0)
        assert entry.is_fresh(now=200.0) is False


# ── _get_or_refresh: hit / miss / expiry ─────────────────────────────────────

class TestGetOrRefresh:
    @pytest.mark.asyncio
    async def test_cold_miss_runs_check_and_caches(self):
        clock = _FakeClock()
        check = AsyncMock(return_value={"status": "ok"})
        with patch.object(hc.time, "monotonic", clock), \
             patch.object(hc, "_check_database_fresh", check):
            out = await hc._get_or_refresh("database")
        assert out == {"status": "ok"}
        check.assert_awaited_once()
        assert "database" in hc._cache

    @pytest.mark.asyncio
    async def test_hit_within_ttl_does_not_recheck(self):
        clock = _FakeClock()
        check = AsyncMock(return_value={"status": "ok"})
        with patch.object(hc.time, "monotonic", clock), \
             patch.object(hc, "_check_database_fresh", check):
            await hc._get_or_refresh("database")
            clock.advance(hc.TTL_SECONDS - 0.1)  # still inside the window
            out = await hc._get_or_refresh("database")
        assert out == {"status": "ok"}
        check.assert_awaited_once()  # second call served from cache

    @pytest.mark.asyncio
    async def test_expiry_triggers_recheck(self):
        clock = _FakeClock()
        check = AsyncMock(side_effect=[{"status": "ok"}, {"status": "down"}])
        with patch.object(hc.time, "monotonic", clock), \
             patch.object(hc, "_check_redis_fresh", check):
            first = await hc._get_or_refresh("redis")
            clock.advance(hc.TTL_SECONDS + 0.5)  # past expiry
            second = await hc._get_or_refresh("redis")
        assert first == {"status": "ok"}
        assert second == {"status": "down"}
        assert check.await_count == 2

    @pytest.mark.asyncio
    async def test_concurrent_stale_callers_refresh_once(self):
        """Double-check lock: many concurrent misses → exactly one check."""
        clock = _FakeClock()
        calls = 0

        async def slow_check():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)  # yield so others queue on the lock
            return {"status": "ok"}

        with patch.object(hc.time, "monotonic", clock), \
             patch.object(hc, "_check_qdrant_fresh", AsyncMock(side_effect=slow_check)):
            await asyncio.gather(*[hc._get_or_refresh("qdrant") for _ in range(8)])
        assert calls == 1  # the other 7 saw the freshly-cached entry


# ── get_cached_health roll-up ────────────────────────────────────────────────

class TestGetCachedHealth:
    @pytest.mark.asyncio
    async def test_all_ok_is_ok(self):
        with patch.object(hc, "_check_database_fresh",
                          AsyncMock(return_value={"status": "ok", "pool_size": 5})), \
             patch.object(hc, "_check_redis_fresh", AsyncMock(return_value={"status": "ok"})), \
             patch.object(hc, "_check_qdrant_fresh", AsyncMock(return_value={"status": "ok"})):
            out = await hc.get_cached_health()
        assert out["status"] == "ok"
        assert out["service"] == "companion-core"
        assert out["database"]["pool_size"] == 5
        assert out["redis"] == "ok"
        assert out["qdrant"] == "ok"
        assert out["cache"]["ttl_seconds"] == hc.TTL_SECONDS
        assert set(out["cache"]["entries"]) == {"database", "redis", "qdrant"}

    @pytest.mark.asyncio
    async def test_db_ok_but_backend_down_is_degraded(self):
        with patch.object(hc, "_check_database_fresh", AsyncMock(return_value={"status": "ok"})), \
             patch.object(hc, "_check_redis_fresh", AsyncMock(return_value={"status": "down"})), \
             patch.object(hc, "_check_qdrant_fresh", AsyncMock(return_value={"status": "ok"})):
            out = await hc.get_cached_health()
        assert out["status"] == "degraded"
        assert out["redis"] == "down"

    @pytest.mark.asyncio
    async def test_db_down_is_unhealthy(self):
        with patch.object(hc, "_check_database_fresh", AsyncMock(return_value={"status": "error"})), \
             patch.object(hc, "_check_redis_fresh", AsyncMock(return_value={"status": "ok"})), \
             patch.object(hc, "_check_qdrant_fresh", AsyncMock(return_value={"status": "ok"})):
            out = await hc.get_cached_health()
        assert out["status"] == "unhealthy"


# ── get_fresh_health ─────────────────────────────────────────────────────────

class TestGetFreshHealth:
    @pytest.mark.asyncio
    async def test_invalidates_cache_then_refreshes(self):
        db_check = AsyncMock(side_effect=[{"status": "ok"}, {"status": "error"}])
        with patch.object(hc, "_check_database_fresh", db_check), \
             patch.object(hc, "_check_redis_fresh", AsyncMock(return_value={"status": "ok"})), \
             patch.object(hc, "_check_qdrant_fresh", AsyncMock(return_value={"status": "ok"})):
            first = await hc.get_cached_health()
            # Without a clock advance a normal call would hit cache; fresh forces refresh.
            second = await hc.get_fresh_health()
        assert first["status"] == "ok"
        assert second["status"] == "unhealthy"  # second db check returned error
        assert db_check.await_count == 2


# ── per-subsystem fresh checks (down/degraded branches) ──────────────────────

class TestFreshCheckImplementations:
    @pytest.mark.asyncio
    async def test_database_fresh_delegates_to_db_health(self):
        with patch("app.db.check_health", AsyncMock(return_value={"status": "ok"})):
            out = await hc._check_database_fresh()
        assert out == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_redis_fresh_ok_on_ping(self):
        # health_cache does `import redis.asyncio as aioredis` then
        # aioredis.from_url(...). Since `redis` is already imported, patch the
        # real submodule's from_url rather than swapping sys.modules.
        import redis.asyncio as aioredis
        fake_redis = MagicMock()
        fake_redis.ping = AsyncMock(return_value=True)
        fake_redis.aclose = AsyncMock()
        with patch.object(hc, "PING_TIMEOUT_SECONDS", 60), \
             patch.object(aioredis, "from_url", MagicMock(return_value=fake_redis)), \
             patch("app.memory.REDIS_URL", "redis://x:6379/0"):
            out = await hc._check_redis_fresh()
        assert out == {"status": "ok"}
        fake_redis.ping.assert_awaited_once()
        fake_redis.aclose.assert_awaited_once()  # client always closed

    @pytest.mark.asyncio
    async def test_redis_fresh_down_on_ping_failure(self):
        import redis.asyncio as aioredis
        fake_redis = MagicMock()
        fake_redis.ping = AsyncMock(side_effect=RuntimeError("conn refused"))
        fake_redis.aclose = AsyncMock()
        with patch.object(hc, "PING_TIMEOUT_SECONDS", 60), \
             patch.object(aioredis, "from_url", MagicMock(return_value=fake_redis)), \
             patch("app.memory.REDIS_URL", "redis://x:6379/0"):
            out = await hc._check_redis_fresh()
        assert out == {"status": "down"}
        fake_redis.aclose.assert_awaited_once()  # closed even on failure

    @pytest.mark.asyncio
    async def test_qdrant_fresh_ok_on_200(self):
        class _R:
            status_code = 200

        class _FakeAsyncClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def get(self, url):
                return _R()

        with patch.object(hc.httpx, "AsyncClient", _FakeAsyncClient):
            out = await hc._check_qdrant_fresh()
        assert out == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_qdrant_fresh_down_on_non_200(self):
        class _R:
            status_code = 503

        class _FakeAsyncClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def get(self, url):
                return _R()

        with patch.object(hc.httpx, "AsyncClient", _FakeAsyncClient):
            out = await hc._check_qdrant_fresh()
        assert out == {"status": "down"}

    @pytest.mark.asyncio
    async def test_qdrant_fresh_down_on_exception(self):
        class _FakeAsyncClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                raise RuntimeError("connect fail")

            async def __aexit__(self, *a):
                return None

        with patch.object(hc.httpx, "AsyncClient", _FakeAsyncClient):
            out = await hc._check_qdrant_fresh()
        assert out == {"status": "down"}


# ── get_live_health + clear_cache ────────────────────────────────────────────

class TestLiveHealthAndClear:
    def test_live_health_is_process_only(self):
        out = hc.get_live_health()
        assert out == {
            "status": "ok",
            "service": "companion-core",
            "version": "0.1.0",
        }

    def test_clear_cache_empties_entries(self):
        hc._cache["database"] = hc._CacheEntry(status={"status": "ok"}, captured_at=1.0)
        assert hc._cache
        hc.clear_cache()
        assert hc._cache == {}
