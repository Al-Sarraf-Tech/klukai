"""Tests for rate_limit.py and request-ID + rate-limit middlewares."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ═══════════════════════════════════════════════════════════════════════════
# rate_limit module
# ═══════════════════════════════════════════════════════════════════════════


class TestLimitsRegistry:
    def test_default_bucket_exists(self):
        from app.rate_limit import LIMITS
        assert "default" in LIMITS

    def test_known_buckets_defined(self):
        from app.rate_limit import LIMITS
        for bucket in ("login", "export", "tts", "stt", "image_gen",
                       "gift", "mission", "search", "stats"):
            assert bucket in LIMITS, f"bucket '{bucket}' missing"

    def test_login_limit_tight(self):
        """Login should be tight (prevent brute-force)."""
        from app.rate_limit import LIMITS
        assert LIMITS["login"].requests <= 20
        assert LIMITS["login"].window_seconds >= 60

    def test_export_limit_tight(self):
        """Export should be very tight (expensive query)."""
        from app.rate_limit import LIMITS
        assert LIMITS["export"].requests <= 5


class TestCheckAndConsume:
    @pytest.mark.asyncio
    async def test_first_call_returns_remaining_minus_one(self):
        from app.rate_limit import check_and_consume, LIMITS
        fake_redis = AsyncMock()
        fake_redis.incr = AsyncMock(return_value=1)
        fake_redis.expire = AsyncMock(return_value=True)
        fake_redis.ttl = AsyncMock(return_value=60)

        with patch("app.rate_limit._get_redis", return_value=fake_redis):
            remaining, retry = await check_and_consume("alice", "stats")

        assert remaining == LIMITS["stats"].requests - 1
        assert retry >= 0
        fake_redis.expire.assert_called_once()

    @pytest.mark.asyncio
    async def test_subsequent_call_decrements(self):
        from app.rate_limit import check_and_consume
        fake_redis = AsyncMock()
        fake_redis.incr = AsyncMock(return_value=5)
        fake_redis.expire = AsyncMock(return_value=True)
        fake_redis.ttl = AsyncMock(return_value=45)

        with patch("app.rate_limit._get_redis", return_value=fake_redis):
            remaining, retry = await check_and_consume("alice", "stats")

        # expire only set on the first call (count==1)
        fake_redis.expire.assert_not_called()
        assert retry == 45

    @pytest.mark.asyncio
    async def test_exceeded_raises(self):
        from app.rate_limit import check_and_consume, RateLimitExceeded, LIMITS
        fake_redis = AsyncMock()
        over = LIMITS["stats"].requests + 1
        fake_redis.incr = AsyncMock(return_value=over)
        fake_redis.expire = AsyncMock(return_value=True)
        fake_redis.ttl = AsyncMock(return_value=30)

        with patch("app.rate_limit._get_redis", return_value=fake_redis):
            with pytest.raises(RateLimitExceeded) as exc:
                await check_and_consume("alice", "stats")

        assert exc.value.bucket == "stats"
        assert exc.value.retry_after >= 1

    @pytest.mark.asyncio
    async def test_fails_open_on_redis_error(self):
        """Redis outage should NOT block requests (fail-open)."""
        from app.rate_limit import check_and_consume

        async def broken():
            raise RuntimeError("redis down")

        with patch("app.rate_limit._get_redis", side_effect=broken):
            remaining, retry = await check_and_consume("alice", "stats")

        # Fail-open returns 999, 0
        assert remaining == 999
        assert retry == 0

    @pytest.mark.asyncio
    async def test_unknown_bucket_uses_default(self):
        from app.rate_limit import check_and_consume, LIMITS
        fake_redis = AsyncMock()
        fake_redis.incr = AsyncMock(return_value=1)
        fake_redis.expire = AsyncMock(return_value=True)
        fake_redis.ttl = AsyncMock(return_value=60)

        with patch("app.rate_limit._get_redis", return_value=fake_redis):
            remaining, _ = await check_and_consume("alice", "no-such-bucket")

        # Uses default limit
        assert remaining == LIMITS["default"].requests - 1


class TestReset:
    @pytest.mark.asyncio
    async def test_reset_deletes_key(self):
        from app.rate_limit import reset
        fake_redis = AsyncMock()
        fake_redis.delete = AsyncMock(return_value=1)

        with patch("app.rate_limit._get_redis", return_value=fake_redis):
            await reset("alice", "stats")

        fake_redis.delete.assert_called_once()
        # Key format includes bucket name
        assert "stats" in fake_redis.delete.call_args[0][0]

    @pytest.mark.asyncio
    async def test_reset_swallows_redis_errors(self):
        from app.rate_limit import reset

        async def broken():
            raise RuntimeError("down")

        # Should not raise
        with patch("app.rate_limit._get_redis", side_effect=broken):
            await reset("alice", "stats")


# ═══════════════════════════════════════════════════════════════════════════
# Bucket-path routing (middleware helper)
# ═══════════════════════════════════════════════════════════════════════════


class TestBucketResolution:
    def test_login_path(self):
        from app.main import _bucket_for_path
        assert _bucket_for_path("/api/auth/login") == "login"

    def test_export_path(self):
        from app.main import _bucket_for_path
        assert _bucket_for_path("/api/user/export") == "export"

    def test_stats_path(self):
        from app.main import _bucket_for_path
        assert _bucket_for_path("/api/user/stats") == "stats"

    def test_memory_search_path(self):
        from app.main import _bucket_for_path
        assert _bucket_for_path("/api/memories/search") == "search"

    def test_unprotected_path_returns_none(self):
        from app.main import _bucket_for_path
        assert _bucket_for_path("/health") is None
        assert _bucket_for_path("/api/messages") is None

    def test_longest_prefix_wins(self):
        """Ensure /api/user/export wins over less specific prefix."""
        from app.main import _bucket_for_path
        assert _bucket_for_path("/api/user/export") == "export"
        assert _bucket_for_path("/api/user/stats") == "stats"

    def test_trailing_segments_allowed(self):
        """Subpaths under a known bucket inherit the bucket."""
        from app.main import _bucket_for_path
        assert _bucket_for_path("/api/tts/anything") == "tts"
