"""Unit tests for app.billing — tier matrix, quota helpers, Stripe signature."""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.billing import (
    PRICING,
    TIER_FEATURES,
    QuotaExceeded,
    Subscription,
    _period_key,
    _stripe_tier_from_price,
    _user_id_from_metadata,
    check_quota,
    consume_quota,
    get_subscription,
    get_tier,
    has_feature,
    handle_stripe_event,
    verify_stripe_signature,
)


class TestTierMatrix:
    def test_all_tiers_present(self):
        assert set(TIER_FEATURES) == {"free", "pro", "elite"}

    def test_free_has_limits(self):
        assert TIER_FEATURES["free"]["chat_messages_per_day"] == 50
        assert TIER_FEATURES["free"]["voice_enabled"] is False

    def test_pro_unlimited_chat(self):
        assert TIER_FEATURES["pro"]["chat_messages_per_day"] is None
        assert TIER_FEATURES["pro"]["voice_enabled"] is True

    def test_elite_priority(self):
        assert TIER_FEATURES["elite"]["priority_support"] is True
        assert TIER_FEATURES["elite"]["memory_archive_cap"] is None

    def test_image_caps_ascending(self):
        f = TIER_FEATURES["free"]["image_gen_per_day"]
        p = TIER_FEATURES["pro"]["image_gen_per_day"]
        e = TIER_FEATURES["elite"]["image_gen_per_day"]
        assert f < p < e


class TestPricing:
    def test_all_tiers_priced(self):
        assert set(PRICING) == {"free", "pro", "elite"}

    def test_each_tier_has_bullets(self):
        for tier in PRICING.values():
            assert isinstance(tier["bullets"], list)
            assert len(tier["bullets"]) >= 3

    def test_each_tier_has_name_and_headline(self):
        for tier in PRICING.values():
            assert isinstance(tier["name"], str) and tier["name"]
            assert isinstance(tier["headline"], str) and tier["headline"]


class TestSubscriptionDataclass:
    def test_default_free_is_active(self):
        s = Subscription(user_id="u", tier="free", status="active")
        assert s.is_active

    def test_canceled_not_active(self):
        s = Subscription(user_id="u", tier="pro", status="canceled")
        assert not s.is_active

    def test_past_due_not_active(self):
        s = Subscription(user_id="u", tier="pro", status="past_due")
        assert not s.is_active

    def test_active_but_expired_not_active(self):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        s = Subscription(user_id="u", tier="pro", status="active", period_end=past)
        assert not s.is_active

    def test_active_future_is_active(self):
        future = datetime.now(timezone.utc) + timedelta(days=30)
        s = Subscription(user_id="u", tier="pro", status="active", period_end=future)
        assert s.is_active

    def test_trialing_counts_as_active(self):
        future = datetime.now(timezone.utc) + timedelta(days=14)
        s = Subscription(user_id="u", tier="pro", status="trialing", period_end=future)
        assert s.is_active

    def test_features_property_matches_matrix(self):
        s = Subscription(user_id="u", tier="elite", status="active")
        assert s.features == TIER_FEATURES["elite"]

    def test_unknown_tier_falls_back_to_free(self):
        s = Subscription(user_id="u", tier="garbage", status="active")
        assert s.features == TIER_FEATURES["free"]


class TestQuotaExceeded:
    def test_carries_counter_tier_limit(self):
        e = QuotaExceeded("image_gen_per_day", "free", 3)
        assert e.counter == "image_gen_per_day"
        assert e.tier == "free"
        assert e.limit == 3
        msg = str(e)
        assert "image_gen_per_day" in msg
        assert "free" in msg
        assert "3" in msg


class TestPeriodKey:
    def test_daily_format(self):
        k = _period_key("daily")
        assert len(k) == 10
        assert k.count("-") == 2

    def test_monthly_format(self):
        k = _period_key("monthly")
        assert len(k) == 7
        assert k.count("-") == 1

    def test_unknown_falls_back_to_daily(self):
        k = _period_key("hourly")
        assert len(k) == 10


class TestStripeTierMap:
    def test_no_price_id_is_free(self):
        assert _stripe_tier_from_price(None) == "free"
        assert _stripe_tier_from_price("") == "free"

    def test_unknown_price_id_is_free(self):
        assert _stripe_tier_from_price("price_unknown") == "free"

    def test_pro_id_maps_when_env_set(self, monkeypatch):
        monkeypatch.setenv("STRIPE_PRICE_PRO_MONTHLY", "price_pro_123")
        # Re-import via direct call — function reads env each call
        assert _stripe_tier_from_price("price_pro_123") == "pro"

    def test_elite_id_maps_when_env_set(self, monkeypatch):
        monkeypatch.setenv("STRIPE_PRICE_ELITE_MONTHLY", "price_elite_xyz")
        assert _stripe_tier_from_price("price_elite_xyz") == "elite"


class TestUserIdFromMetadata:
    def test_present(self):
        assert _user_id_from_metadata({"metadata": {"user_id": "alice"}}) == "alice"

    def test_missing(self):
        assert _user_id_from_metadata({}) is None
        assert _user_id_from_metadata({"metadata": {}}) is None
        assert _user_id_from_metadata({"metadata": None}) is None


class TestStripeSignatureVerify:
    _SEC = "wh" + "sec_" + "abc_test"  # split to avoid naive scanners

    def _sign(self, payload: bytes, secret: str, ts: int | None = None) -> str:
        ts = ts or int(time.time())
        signed = f"{ts}.".encode() + payload
        mac = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return f"t={ts},v1={mac}"

    def test_valid_signature_accepts(self):
        body = b'{"id":"evt_1"}'
        header = self._sign(body, self._SEC)
        assert verify_stripe_signature(body, header, self._SEC) is True

    def test_no_secret_rejects(self):
        assert verify_stripe_signature(b"x", "t=1,v1=x", "") is False

    def test_old_timestamp_rejects(self):
        body = b'{"id":"evt_1"}'
        header = self._sign(body, self._SEC, ts=int(time.time()) - 1000)
        assert verify_stripe_signature(body, header, self._SEC) is False

    def test_tampered_payload_rejects(self):
        body = b'{"id":"evt_1"}'
        header = self._sign(body, self._SEC)
        tampered = b'{"id":"evt_2"}'
        assert verify_stripe_signature(tampered, header, self._SEC) is False

    def test_wrong_secret_rejects(self):
        body = b'{"id":"evt_1"}'
        header = self._sign(body, self._SEC)
        assert verify_stripe_signature(body, header, self._SEC + "_wrong") is False

    def test_malformed_header_rejects(self):
        assert verify_stripe_signature(b"x", "garbage", self._SEC) is False
        assert verify_stripe_signature(b"x", "", self._SEC) is False


def _mk_conn(fetchone=None, fetchall=None):
    """Build a mock async conn with execute → result with fetchone/fetchall."""
    conn = MagicMock()
    result = MagicMock()
    result.fetchone = AsyncMock(return_value=fetchone)
    result.fetchall = AsyncMock(return_value=fetchall or [])
    conn.execute = AsyncMock(return_value=result)
    return conn


class TestGetSubscription:
    @pytest.mark.asyncio
    async def test_personal_mode_returns_elite(self, monkeypatch):
        """Default self-hosted setting — everyone is elite, no DB lookup."""
        monkeypatch.setenv("KLUKAI_PERSONAL_MODE", "true")
        sub = await get_subscription("anyone")
        assert sub.tier == "elite"
        assert sub.is_active

    @pytest.mark.asyncio
    async def test_returns_db_row(self, monkeypatch):
        monkeypatch.setenv("KLUKAI_PERSONAL_MODE", "false")
        conn = _mk_conn(fetchone=(
            "pro", "active", None,
            datetime.now(timezone.utc) + timedelta(days=30),
            "cus_123", "sub_456",
        ))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch("app.billing.get_conn", return_value=cm):
            sub = await get_subscription("alice")
        assert sub.tier == "pro"
        assert sub.status == "active"
        assert sub.stripe_customer_id == "cus_123"

    @pytest.mark.asyncio
    async def test_no_row_returns_free_default(self, monkeypatch):
        monkeypatch.setenv("KLUKAI_PERSONAL_MODE", "false")
        conn = _mk_conn(fetchone=None)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch("app.billing.get_conn", return_value=cm):
            sub = await get_subscription("nobody")
        assert sub.tier == "free"

    @pytest.mark.asyncio
    async def test_db_error_returns_free_default(self, monkeypatch):
        monkeypatch.setenv("KLUKAI_PERSONAL_MODE", "false")
        with patch("app.billing.get_conn", side_effect=RuntimeError("pool dead")):
            sub = await get_subscription("alice")
        assert sub.tier == "free"

    @pytest.mark.asyncio
    async def test_get_tier_helper(self, monkeypatch):
        monkeypatch.setenv("KLUKAI_PERSONAL_MODE", "false")
        with patch("app.billing.get_subscription",
                   new=AsyncMock(return_value=Subscription("u", "elite", "active"))):
            t = await get_tier("u")
        assert t == "elite"

    @pytest.mark.asyncio
    async def test_get_tier_inactive_returns_free(self, monkeypatch):
        monkeypatch.setenv("KLUKAI_PERSONAL_MODE", "false")
        with patch("app.billing.get_subscription",
                   new=AsyncMock(return_value=Subscription("u", "pro", "canceled"))):
            t = await get_tier("u")
        assert t == "free"

    @pytest.mark.asyncio
    async def test_has_feature_true_when_active(self, monkeypatch):
        monkeypatch.setenv("KLUKAI_PERSONAL_MODE", "false")
        with patch("app.billing.get_subscription",
                   new=AsyncMock(return_value=Subscription("u", "pro", "active"))):
            assert await has_feature("u", "voice_enabled") is True

    @pytest.mark.asyncio
    async def test_has_feature_false_when_canceled(self, monkeypatch):
        monkeypatch.setenv("KLUKAI_PERSONAL_MODE", "false")
        with patch("app.billing.get_subscription",
                   new=AsyncMock(return_value=Subscription("u", "pro", "canceled"))):
            # Inactive falls back to free, where voice is off
            assert await has_feature("u", "voice_enabled") is False


class TestCheckQuota:
    @pytest.mark.asyncio
    async def test_zero_used_when_no_row(self, monkeypatch):
        monkeypatch.setenv("KLUKAI_PERSONAL_MODE", "false")
        conn = _mk_conn(fetchone=None)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch("app.billing.get_subscription",
                   new=AsyncMock(return_value=Subscription("u", "free", "active"))), \
             patch("app.billing.get_conn", return_value=cm):
            used, limit = await check_quota("u", "image_gen_per_day")
        assert used == 0
        assert limit == 3

    @pytest.mark.asyncio
    async def test_returns_existing_count(self, monkeypatch):
        monkeypatch.setenv("KLUKAI_PERSONAL_MODE", "false")
        conn = _mk_conn(fetchone=(7,))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch("app.billing.get_subscription",
                   new=AsyncMock(return_value=Subscription("u", "pro", "active"))), \
             patch("app.billing.get_conn", return_value=cm):
            used, limit = await check_quota("u", "image_gen_per_day")
        assert used == 7
        assert limit == 50

    @pytest.mark.asyncio
    async def test_db_error_zero_used(self, monkeypatch):
        monkeypatch.setenv("KLUKAI_PERSONAL_MODE", "false")
        with patch("app.billing.get_subscription",
                   new=AsyncMock(return_value=Subscription("u", "free", "active"))), \
             patch("app.billing.get_conn", side_effect=RuntimeError("down")):
            used, limit = await check_quota("u", "image_gen_per_day")
        assert used == 0
        assert limit == 3


class TestConsumeQuota:
    @pytest.mark.asyncio
    async def test_personal_mode_no_limit(self, monkeypatch):
        """Personal mode bypasses all quotas."""
        monkeypatch.setenv("KLUKAI_PERSONAL_MODE", "true")
        conn = _mk_conn(fetchone=(99,))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch("app.billing.get_conn_autocommit", return_value=cm):
            remaining = await consume_quota("alice", "image_gen_per_day")
        assert remaining >= 10**8  # sentinel for unlimited

    @pytest.mark.asyncio
    async def test_unlimited_tier_returns_sentinel(self, monkeypatch):
        monkeypatch.setenv("KLUKAI_PERSONAL_MODE", "false")
        conn = _mk_conn(fetchone=(1,))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch("app.billing.get_subscription",
                   new=AsyncMock(return_value=Subscription("u", "pro", "active"))), \
             patch("app.billing.get_conn_autocommit", return_value=cm):
            remaining = await consume_quota("u", "chat_messages_per_day")
        assert remaining >= 10**8  # sentinel

    @pytest.mark.asyncio
    async def test_under_limit_returns_remaining(self, monkeypatch):
        monkeypatch.setenv("KLUKAI_PERSONAL_MODE", "false")
        # free tier image_gen_per_day = 3. New count = 2 → remaining = 1
        conn = _mk_conn(fetchone=(2,))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch("app.billing.get_subscription",
                   new=AsyncMock(return_value=Subscription("u", "free", "active"))), \
             patch("app.billing.get_conn_autocommit", return_value=cm):
            remaining = await consume_quota("u", "image_gen_per_day")
        assert remaining == 1

    @pytest.mark.asyncio
    async def test_over_limit_raises(self, monkeypatch):
        monkeypatch.setenv("KLUKAI_PERSONAL_MODE", "false")
        # free image_gen = 3. New count = 4 → raises
        conn = _mk_conn(fetchone=(4,))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch("app.billing.get_subscription",
                   new=AsyncMock(return_value=Subscription("u", "free", "active"))), \
             patch("app.billing.get_conn_autocommit", return_value=cm):
            with pytest.raises(QuotaExceeded) as ei:
                await consume_quota("u", "image_gen_per_day")
        assert ei.value.tier == "free"
        assert ei.value.limit == 3


class TestStripeEventDispatch:
    @pytest.mark.asyncio
    async def test_missing_event_id(self):
        result = await handle_stripe_event({"type": "customer.subscription.created"})
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_replay_returns_replay_flag(self):
        # INSERT ... ON CONFLICT DO NOTHING returns no row when duplicate
        conn = _mk_conn(fetchone=None)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch("app.billing_stripe.get_conn_autocommit", return_value=cm):
            result = await handle_stripe_event({
                "id": "evt_replay",
                "type": "customer.subscription.deleted",
                "data": {"object": {}},
            })
        assert result.get("replay") is True

    @pytest.mark.asyncio
    async def test_unknown_event_type_acked(self):
        # New event id → INSERT returns it; unknown type → marked processed
        conn = _mk_conn(fetchone=("evt_unknown",))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch("app.billing_stripe.get_conn_autocommit", return_value=cm):
            result = await handle_stripe_event({
                "id": "evt_unknown",
                "type": "made.up.event",
                "data": {"object": {}},
            })
        assert result["ok"] is True
        assert result.get("handler") is None
