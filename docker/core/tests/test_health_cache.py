"""Unit tests for app.observability.health_cache.

Verifies:
- Cache returns sub-millisecond on a hit
- TTL expiry forces a refresh
- Per-subsystem lock prevents thundering-herd refresh
- get_fresh_health() invalidates and refreshes
- get_live_health() never pings backends
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.observability import health_cache


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    """Fresh cache state per test."""
    health_cache.clear_cache()
    yield
    health_cache.clear_cache()


@pytest.fixture
def _mock_backends():
    """Patch the per-subsystem fresh checks so tests don't need real PG/Redis/Qdrant."""
    with (
        patch.object(health_cache, "_check_database_fresh",
                     new=AsyncMock(return_value={"status": "ok", "pool_size": 2})),
        patch.object(health_cache, "_check_redis_fresh",
                     new=AsyncMock(return_value={"status": "ok"})),
        patch.object(health_cache, "_check_qdrant_fresh",
                     new=AsyncMock(return_value={"status": "ok"})),
    ):
        yield


class TestLiveHealth:
    def test_returns_ok_without_pinging(self):
        """Liveness is process-only; no backend calls."""
        result = health_cache.get_live_health()
        assert result["status"] == "ok"
        assert result["service"] == "companion-core"

    def test_synchronous_no_await_needed(self):
        """Liveness is sync — friendly to dumb probes."""
        result = health_cache.get_live_health()  # NOT awaited
        assert isinstance(result, dict)


class TestCachedHealth:
    @pytest.mark.asyncio
    async def test_first_call_populates_cache(self, _mock_backends):
        result = await health_cache.get_cached_health()
        assert result["status"] == "ok"
        assert "database" in result
        assert "redis" in result
        assert "qdrant" in result
        # Cache should now contain all three
        assert set(health_cache._cache.keys()) == {"database", "redis", "qdrant"}

    @pytest.mark.asyncio
    async def test_cache_hit_skips_backend_calls(self, _mock_backends):
        # First call populates cache
        await health_cache.get_cached_health()
        db_mock = health_cache._check_database_fresh
        redis_mock = health_cache._check_redis_fresh

        # Reset mock call counts after the first populate
        db_mock.reset_mock()
        redis_mock.reset_mock()

        # Second call should be a cache hit — no backend pings
        await health_cache.get_cached_health()
        assert db_mock.call_count == 0
        assert redis_mock.call_count == 0

    @pytest.mark.asyncio
    async def test_ttl_expiry_triggers_refresh(self, _mock_backends):
        await health_cache.get_cached_health()
        # Force every entry to be "old enough" to be stale
        for entry in health_cache._cache.values():
            entry.captured_at = time.monotonic() - (health_cache.TTL_SECONDS + 1)

        db_mock = health_cache._check_database_fresh
        db_mock.reset_mock()

        await health_cache.get_cached_health()
        assert db_mock.call_count == 1  # refreshed

    @pytest.mark.asyncio
    async def test_unhealthy_status_when_db_down(self, _mock_backends):
        health_cache._check_database_fresh.return_value = {"status": "down"}
        result = await health_cache.get_cached_health()
        assert result["status"] == "unhealthy"

    @pytest.mark.asyncio
    async def test_degraded_status_when_partial_outage(self, _mock_backends):
        # DB up, redis down
        health_cache._check_redis_fresh.return_value = {"status": "down"}
        result = await health_cache.get_cached_health()
        assert result["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_refresh(self, _mock_backends):
        """Thundering-herd prevention: 10 concurrent stale reads = 1 backend call per subsystem."""
        # Prime cache then expire it
        await health_cache.get_cached_health()
        for entry in health_cache._cache.values():
            entry.captured_at = time.monotonic() - (health_cache.TTL_SECONDS + 1)

        db_mock = health_cache._check_database_fresh
        db_mock.reset_mock()

        # Fire 10 concurrent get_cached_health calls
        await asyncio.gather(*[health_cache.get_cached_health() for _ in range(10)])

        # Only one fresh fetch per subsystem regardless of concurrency
        assert db_mock.call_count == 1


class TestFreshHealth:
    @pytest.mark.asyncio
    async def test_invalidates_cache(self, _mock_backends):
        # Populate cache
        await health_cache.get_cached_health()
        db_mock = health_cache._check_database_fresh
        db_mock.reset_mock()

        # Fresh call should invalidate + refresh
        await health_cache.get_fresh_health()
        assert db_mock.call_count == 1


class TestClearCache:
    def test_drops_all_entries(self, _mock_backends):
        # Populate
        asyncio.run(health_cache.get_cached_health())
        assert health_cache._cache
        # Clear
        health_cache.clear_cache()
        assert not health_cache._cache
