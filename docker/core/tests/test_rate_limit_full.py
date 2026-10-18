"""Tests for app.rate_limit — Redis-backed token bucket limiter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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


class _Pipe:
    """A MULTI/EXEC pipeline double: records queued commands, returns the
    scripted transaction result (count, armed, ttl, expiretime)."""

    def __init__(self, results, error=None):
        self.results, self.error, self.calls = results, error, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def incr(self, key):
        self.calls.append(("incr", key))
        return self

    def expire(self, key, seconds, nx=False):
        self.calls.append(("expire", key, seconds, nx))
        return self

    def ttl(self, key):
        self.calls.append(("ttl", key))
        return self

    def expiretime(self, key):
        self.calls.append(("expiretime", key))
        return self

    async def execute(self):
        if self.error:
            raise self.error
        return list(self.results)


def _redis(count, ttl=60, expires_at=1_800_000_000, armed=True, error=None):
    pipe = _Pipe([count, armed, ttl, expires_at], error)
    r = MagicMock()
    r.pipeline = MagicMock(return_value=pipe)
    return r, pipe


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
        with patch("app.rate_limit._get_redis", side_effect=RuntimeError("redis down")):
            remaining, retry = await check_and_consume("alice", "default")
        assert (remaining, retry) == (999, 0)

    @pytest.mark.asyncio
    async def test_transaction_failure_fails_open(self):
        r, _ = _redis(1, error=RuntimeError("OOM"))
        with patch("app.rate_limit._get_redis", return_value=r):
            remaining, retry = await check_and_consume("alice", "login")
        assert (remaining, retry) == (999, 0)


class TestCheckAndConsumeIsOneTransaction:
    """INCR, EXPIRE NX, TTL and EXPIRETIME run in one MULTI/EXEC: the count and
    the window it belongs to can never come from different windows."""

    @pytest.mark.asyncio
    async def test_commands_and_transaction(self):
        r, pipe = _redis(1, ttl=300)
        with patch("app.rate_limit._get_redis", return_value=r):
            remaining, retry = await check_and_consume("alice", "login")
        r.pipeline.assert_called_once_with(transaction=True)
        key = f"{LIMITS['login'].key_prefix}:alice:login"
        assert pipe.calls == [
            ("incr", key),
            ("expire", key, LIMITS["login"].window_seconds, True),
            ("ttl", key),
            ("expiretime", key),
        ]
        assert (remaining, retry) == (LIMITS["login"].requests - 1, 300)

    @pytest.mark.asyncio
    async def test_unknown_bucket_uses_default(self):
        r, _ = _redis(1)
        with patch("app.rate_limit._get_redis", return_value=r):
            remaining, _ = await check_and_consume("alice", "bogus_bucket")
        assert remaining == LIMITS["default"].requests - 1

    @pytest.mark.asyncio
    async def test_live_ttl_is_reported(self):
        r, _ = _redis(2, ttl=42, armed=False)
        with patch("app.rate_limit._get_redis", return_value=r):
            _, retry = await check_and_consume("alice", "login")
        assert retry == 42

    @pytest.mark.asyncio
    async def test_a_counter_without_ttl_is_judged_against_a_full_window(self):
        """EXPIRE NX re-arms a counter whose EXPIRE was lost; should the TTL
        still read -1, the request is judged against a full window, never a
        permanent 429."""
        r, _ = _redis(5, ttl=-1)
        with patch("app.rate_limit._get_redis", return_value=r):
            remaining, retry = await check_and_consume("alice", "login")
        assert remaining == LIMITS["login"].requests - 5
        assert retry == LIMITS["login"].window_seconds


class TestCheckAndConsumeExceeded:
    @pytest.mark.asyncio
    async def test_raises_when_over_limit(self):
        r, _ = _redis(LIMITS["login"].requests + 1, ttl=120)
        with patch("app.rate_limit._get_redis", return_value=r):
            with pytest.raises(RateLimitExceeded) as exc_info:
                await check_and_consume("alice", "login")
        assert exc_info.value.bucket == "login"
        assert exc_info.value.retry_after == 120

    @pytest.mark.asyncio
    async def test_retry_after_clamped_to_1_minimum(self):
        r, _ = _redis(LIMITS["login"].requests + 5, ttl=0)
        with patch("app.rate_limit._get_redis", return_value=r):
            with pytest.raises(RateLimitExceeded) as exc_info:
                await check_and_consume("alice", "login")
        assert exc_info.value.retry_after == 1

    @pytest.mark.asyncio
    async def test_no_ttl_over_limit_retry_after_is_the_window(self):
        r, _ = _redis(LIMITS["login"].requests + 1, ttl=-1)
        with patch("app.rate_limit._get_redis", return_value=r):
            with pytest.raises(RateLimitExceeded) as exc_info:
                await check_and_consume("alice", "login")
        assert exc_info.value.retry_after == LIMITS["login"].window_seconds


class TestConsumeReceipt:
    """consume() hands back a receipt naming the window its token came from."""

    @pytest.mark.asyncio
    async def test_receipt_carries_key_and_window_expiry(self):
        r, _ = _redis(1, ttl=600, expires_at=1_800_000_600)
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
    async def test_a_window_without_expiry_has_no_receipt(self):
        r, _ = _redis(1, ttl=-1, expires_at=-1)
        with patch("app.rate_limit._get_redis", return_value=r):
            _, _, receipt = await consume("alice", "portrait_refresh")
        assert receipt is None

    @pytest.mark.asyncio
    async def test_rejected_request_still_raises(self):
        r, _ = _redis(LIMITS["portrait_refresh"].requests + 1, ttl=120)
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
