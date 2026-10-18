"""Tests for app.rate_limit — Redis-backed token bucket limiter."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app import rate_limit
from app.rate_limit import (
    LIMITS,
    Consumption,
    Limit,
    RateLimitExceeded,
    check_and_consume,
    consume,
    refund,
    reset,
)


@pytest.fixture(autouse=True)
def _reset_redis():
    rate_limit._redis = None
    yield
    rate_limit._redis = None


class TestLimitDataclass:
    def test_immutable(self):
        lim = Limit(requests=10, window_seconds=60)
        with pytest.raises(Exception):
            lim.requests = 20  # frozen

    def test_key_prefix_format(self):
        lim = Limit(requests=60, window_seconds=300)
        assert lim.key_prefix == "ratelimit:60/300"


class TestLIMITS:
    def test_default_present(self):
        assert "default" in LIMITS

    def test_login_tightest(self):
        # Login should have the most restrictive request count to slow
        # brute force; check it's not absurdly high
        assert LIMITS["login"].requests <= 20

    def test_export_lowest_volume(self):
        # Per-hour export limit should be very low
        assert LIMITS["export"].requests <= 5
        assert LIMITS["export"].window_seconds >= 1800

    def test_image_gen_is_hourly(self):
        assert LIMITS["image_gen"].window_seconds == 3600

    def test_all_limits_have_positive_values(self):
        for name, lim in LIMITS.items():
            assert lim.requests > 0
            assert lim.window_seconds > 0


class TestRateLimitExceededException:
    def test_carries_bucket_and_retry(self):
        e = RateLimitExceeded("login", 42)
        assert e.bucket == "login"
        assert e.retry_after == 42
        assert "login" in str(e)
        assert "42" in str(e)


class TestCheckAndConsumeFailOpen:
    @pytest.mark.asyncio
    async def test_redis_unreachable_returns_999_zero(self):
        # Simulate _get_redis raising — falls into the broad except
        with patch("app.rate_limit._get_redis", side_effect=RuntimeError("redis down")):
            remaining, retry = await check_and_consume("alice", "default")
        assert remaining == 999
        assert retry == 0

    @pytest.mark.asyncio
    async def test_redis_op_failure_fail_open(self):
        # _get_redis returns a redis client, but incr() blows up
        r = AsyncMock()
        r.incr = AsyncMock(side_effect=RuntimeError("OOM"))
        with patch("app.rate_limit._get_redis", return_value=r):
            remaining, retry = await check_and_consume("alice", "login")
        assert remaining == 999


class TestCheckAndConsumeSuccess:
    @pytest.mark.asyncio
    async def test_first_call_sets_expire(self):
        r = AsyncMock()
        r.incr = AsyncMock(return_value=1)
        r.expire = AsyncMock()
        r.ttl = AsyncMock(return_value=300)
        with patch("app.rate_limit._get_redis", return_value=r):
            remaining, retry = await check_and_consume("alice", "login")
        assert remaining == LIMITS["login"].requests - 1
        r.expire.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_subsequent_call_no_expire(self):
        r = AsyncMock()
        r.incr = AsyncMock(return_value=5)
        r.expire = AsyncMock()
        r.ttl = AsyncMock(return_value=200)
        with patch("app.rate_limit._get_redis", return_value=r):
            await check_and_consume("alice", "login")
        r.expire.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_bucket_uses_default(self):
        r = AsyncMock()
        r.incr = AsyncMock(return_value=1)
        r.expire = AsyncMock()
        r.ttl = AsyncMock(return_value=60)
        with patch("app.rate_limit._get_redis", return_value=r):
            remaining, retry = await check_and_consume("alice", "bogus_bucket")
        assert remaining == LIMITS["default"].requests - 1


class TestCheckAndConsumeExceeded:
    @pytest.mark.asyncio
    async def test_raises_when_over_limit(self):
        r = AsyncMock()
        r.incr = AsyncMock(return_value=LIMITS["login"].requests + 1)
        r.expire = AsyncMock()
        r.ttl = AsyncMock(return_value=120)
        with patch("app.rate_limit._get_redis", return_value=r):
            with pytest.raises(RateLimitExceeded) as exc_info:
                await check_and_consume("alice", "login")
        assert exc_info.value.bucket == "login"
        assert exc_info.value.retry_after >= 1

    @pytest.mark.asyncio
    async def test_retry_after_clamped_to_1_minimum(self):
        # When TTL is < 1, retry_after still >= 1
        r = AsyncMock()
        r.incr = AsyncMock(return_value=LIMITS["login"].requests + 5)
        r.expire = AsyncMock()
        r.ttl = AsyncMock(return_value=0)  # TTL race condition
        with patch("app.rate_limit._get_redis", return_value=r):
            with pytest.raises(RateLimitExceeded) as exc_info:
                await check_and_consume("alice", "login")
        assert exc_info.value.retry_after == 1


class TestCheckAndConsumeReArmsLostTtl:
    """INCR then EXPIRE is two round trips: if the EXPIRE is lost the key has
    no TTL, the counter never resets, and the bucket is locked for good (a 429
    whose retry_after never clears). A TTL of -1 must re-arm the window."""

    @pytest.mark.asyncio
    async def test_missing_ttl_is_rearmed_and_request_judged_by_count(self):
        r = AsyncMock()
        r.incr = AsyncMock(return_value=5)
        r.expire = AsyncMock()
        r.ttl = AsyncMock(return_value=-1)
        with patch("app.rate_limit._get_redis", return_value=r):
            remaining, retry = await check_and_consume("alice", "login")
        # Under the limit → allowed, by the normal count rule.
        assert remaining == LIMITS["login"].requests - 5
        # Re-armed with the bucket's window, and that window is what's reported.
        r.expire.assert_awaited_once()
        assert r.expire.await_args.args[1] == LIMITS["login"].window_seconds
        assert retry == LIMITS["login"].window_seconds

    @pytest.mark.asyncio
    async def test_missing_ttl_over_limit_retry_after_is_the_window(self):
        r = AsyncMock()
        r.incr = AsyncMock(return_value=LIMITS["login"].requests + 1)
        r.expire = AsyncMock()
        r.ttl = AsyncMock(return_value=-1)
        with patch("app.rate_limit._get_redis", return_value=r):
            with pytest.raises(RateLimitExceeded) as exc_info:
                await check_and_consume("alice", "login")
        # Not a permanent "retry in 1s": the counter now expires with the window.
        assert exc_info.value.retry_after == LIMITS["login"].window_seconds
        r.expire.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_first_hit_whose_expire_did_not_stick_rearms(self):
        r = AsyncMock()
        r.incr = AsyncMock(return_value=1)
        r.expire = AsyncMock()
        r.ttl = AsyncMock(return_value=-1)
        with patch("app.rate_limit._get_redis", return_value=r):
            remaining, _ = await check_and_consume("alice", "login")
        assert remaining == LIMITS["login"].requests - 1
        assert r.expire.await_count == 2  # first-hit arm + re-arm

    @pytest.mark.asyncio
    async def test_live_ttl_is_left_alone(self):
        r = AsyncMock()
        r.incr = AsyncMock(return_value=2)
        r.expire = AsyncMock()
        r.ttl = AsyncMock(return_value=42)
        with patch("app.rate_limit._get_redis", return_value=r):
            _, retry = await check_and_consume("alice", "login")
        r.expire.assert_not_awaited()
        assert retry == 42

    @pytest.mark.asyncio
    async def test_rearm_failure_still_fails_open(self):
        r = AsyncMock()
        r.incr = AsyncMock(return_value=5)
        r.ttl = AsyncMock(return_value=-1)
        r.expire = AsyncMock(side_effect=RuntimeError("READONLY replica"))
        with patch("app.rate_limit._get_redis", return_value=r):
            remaining, retry = await check_and_consume("alice", "login")
        assert (remaining, retry) == (999, 0)


class TestConsumeReceipt:
    """consume() hands back a receipt naming the window its token came from."""

    @pytest.mark.asyncio
    async def test_receipt_carries_key_and_window_expiry(self):
        r = AsyncMock()
        r.incr = AsyncMock(return_value=1)
        r.ttl = AsyncMock(return_value=600)
        r.expiretime = AsyncMock(return_value=1_800_000_600)
        with patch("app.rate_limit._get_redis", return_value=r):
            remaining, retry, receipt = await consume("alice", "portrait_refresh")
        assert (remaining, retry) == (LIMITS["portrait_refresh"].requests - 1, 600)
        assert receipt == Consumption(
            f"{LIMITS['portrait_refresh'].key_prefix}:alice:portrait_refresh",
            1_800_000_600,
        )

    @pytest.mark.asyncio
    async def test_fail_open_has_no_receipt(self):
        with patch("app.rate_limit._get_redis", side_effect=RuntimeError("down")):
            assert await consume("alice", "portrait_refresh") == (999, 0, None)

    @pytest.mark.asyncio
    async def test_unreadable_window_admits_without_a_receipt(self):
        r = AsyncMock()
        r.incr = AsyncMock(return_value=1)
        r.ttl = AsyncMock(return_value=600)
        r.expiretime = AsyncMock(side_effect=RuntimeError("unknown command"))
        with patch("app.rate_limit._get_redis", return_value=r):
            remaining, retry, receipt = await consume("alice", "portrait_refresh")
        assert receipt is None
        assert retry == 600

    @pytest.mark.asyncio
    async def test_rejected_request_still_raises(self):
        r = AsyncMock()
        r.incr = AsyncMock(return_value=LIMITS["portrait_refresh"].requests + 1)
        r.ttl = AsyncMock(return_value=120)
        r.expiretime = AsyncMock(return_value=1_800_000_120)
        with patch("app.rate_limit._get_redis", return_value=r):
            with pytest.raises(RateLimitExceeded) as exc:
                await consume("alice", "portrait_refresh")
        assert exc.value.retry_after == 120


class TestRefund:
    """refund() gives a token back only for a real receipt, atomically."""

    RECEIPT = Consumption("klukai:rl:alice:portrait_refresh", 1_800_000_600)

    @pytest.mark.asyncio
    async def test_no_receipt_refunds_nothing(self):
        with patch("app.rate_limit._get_redis") as get:
            assert await refund(None) is False
        get.assert_not_called()

    @pytest.mark.asyncio
    async def test_runs_the_window_bound_script(self):
        r = AsyncMock()
        r.eval = AsyncMock(return_value=1)
        with patch("app.rate_limit._get_redis", return_value=r):
            assert await refund(self.RECEIPT) is True
        script, numkeys, key, window = r.eval.await_args.args
        assert numkeys == 1
        assert key == self.RECEIPT.key
        assert window == "1800000600"
        assert "EXPIRETIME" in script and "count <= 0" in script and "DECR" in script
        r.decr.assert_not_called()
        r.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_refused_refund_reports_false(self):
        r = AsyncMock()
        r.eval = AsyncMock(return_value=0)
        with patch("app.rate_limit._get_redis", return_value=r):
            assert await refund(self.RECEIPT) is False

    @pytest.mark.asyncio
    async def test_fails_soft_on_redis_error(self):
        with patch("app.rate_limit._get_redis", side_effect=RuntimeError("down")):
            assert await refund(self.RECEIPT) is False

    @pytest.mark.asyncio
    async def test_fails_soft_when_the_script_raises(self):
        r = AsyncMock()
        r.eval = AsyncMock(side_effect=RuntimeError("NOSCRIPT"))
        with patch("app.rate_limit._get_redis", return_value=r):
            assert await refund(self.RECEIPT) is False


class TestReset:
    @pytest.mark.asyncio
    async def test_calls_redis_delete(self):
        r = AsyncMock()
        r.delete = AsyncMock()
        with patch("app.rate_limit._get_redis", return_value=r):
            await reset("alice", "login")
        r.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fails_quietly_on_redis_error(self):
        with patch("app.rate_limit._get_redis", side_effect=RuntimeError("down")):
            # Should not raise
            await reset("alice", "login")

    @pytest.mark.asyncio
    async def test_unknown_bucket_uses_default_key(self):
        r = AsyncMock()
        r.delete = AsyncMock()
        with patch("app.rate_limit._get_redis", return_value=r):
            await reset("alice", "bogus")
        # Key was constructed (no assert on key shape — just no exception)
        r.delete.assert_awaited_once()
