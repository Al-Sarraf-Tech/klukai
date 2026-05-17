"""Monetization SCAFFOLD — dormant by default.

This module defines the data model and feature-gate primitives for a future
paid tier structure. Activation surface (Stripe Checkout, Billing Portal,
subscribe UI buttons) is intentionally absent from public API — billing is
controlled entirely by:

    - KLUKAI_PERSONAL_MODE=true  →  every user is elite, all features on,
                                    quotas disabled. Default for self-hosted.
    - KLUKAI_PERSONAL_MODE=false →  honor companion_subscriptions rows; tier
                                    gates and quotas enforce.
    - Future flip-on: add STRIPE_API_KEY + restore activation endpoints.

SACRED invariants (CLAUDE.md):
- Tier downgrade NEVER deletes chat memories, episodes, affection, Qdrant vectors.
- Subscription cancel only revokes future feature access.
- Stripe webhook validates HMAC; replays are idempotent via event_id PK.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .db import get_conn, get_conn_autocommit


logger = logging.getLogger(__name__)


# ── Personal mode (default ON for self-hosted) ──────────────────────────────


def _personal_mode() -> bool:
    """When true: bypass tier gates, treat everyone as elite, disable quotas.

    Read at every call (not cached) so tests can flip via monkeypatch.
    """
    return os.environ.get("KLUKAI_PERSONAL_MODE", "true").lower() in (
        "1", "true", "yes", "on",
    )


# ── Tier feature matrix ──────────────────────────────────────────────────────
#
# Each tier maps to a dict of caps. None = unlimited. Reset windows are either
# 'daily' (rolling 24h) or 'monthly' (calendar month).

TIER_FEATURES: dict[str, dict[str, Any]] = {
    "free": {
        "chat_messages_per_day": 50,
        "image_gen_per_day": 3,
        "voice_enabled": False,
        "memory_archive_cap": 20,
        "dream_diary_enabled": False,
        "anniversaries_enabled": False,
        "priority_support": False,
    },
    "pro": {
        "chat_messages_per_day": None,
        "image_gen_per_day": 50,
        "voice_enabled": True,
        "memory_archive_cap": 500,
        "dream_diary_enabled": True,
        "anniversaries_enabled": True,
        "priority_support": False,
    },
    "elite": {
        "chat_messages_per_day": None,
        "image_gen_per_day": 250,
        "voice_enabled": True,
        "memory_archive_cap": None,
        "dream_diary_enabled": True,
        "anniversaries_enabled": True,
        "priority_support": True,
    },
}


# ── Pricing surface (DORMANT — only used when activation is restored) ──────
#
# Kept as a constant so the data model + future activation know the intended
# tier boundaries. Not exposed via any public API while
# KLUKAI_PERSONAL_MODE=true (the self-hosted default).


PRICING: dict[str, dict[str, Any]] = {
    "free": {
        "name": "Free",
        "headline": "Personal mode — every memory preserved forever.",
        "bullets": [
            "Daily chat budget",
            "Limited image generation",
            "Small photo album",
            "Affection + persistence (no time limit)",
        ],
    },
    "pro": {
        "name": "Pro",
        "headline": "Unlimited chat, voice in Japanese, expanded memory.",
        "bullets": [
            "Unlimited messages",
            "Generous image budget",
            "Japanese voice replies",
            "Expanded photo album",
            "Dream diary + anniversary tracking",
        ],
    },
    "elite": {
        "name": "Elite",
        "headline": "Every feature, no limits, priority response queue.",
        "bullets": [
            "Everything in Pro",
            "Largest image budget",
            "Unlimited memory archive",
            "Priority response queue",
            "Direct support",
        ],
    },
}


# ── Data model ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Subscription:
    user_id: str
    tier: str
    status: str
    period_start: datetime | None = None
    period_end: datetime | None = None
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None

    @property
    def is_active(self) -> bool:
        return self.status in ("active", "trialing") and (
            self.period_end is None or self.period_end > datetime.now(timezone.utc)
        )

    @property
    def features(self) -> dict[str, Any]:
        return TIER_FEATURES.get(self.tier, TIER_FEATURES["free"])


class QuotaExceeded(Exception):
    """Raised when a feature is used past its tier cap."""

    def __init__(self, counter: str, tier: str, limit: int):
        super().__init__(
            f"Quota exceeded: {counter} (tier={tier}, limit={limit}/period)"
        )
        self.counter = counter
        self.tier = tier
        self.limit = limit


# ── Subscription lookup + tier helpers ──────────────────────────────────────


async def get_subscription(user_id: str) -> Subscription:
    """Return the user's current subscription.

    Personal mode: always returns elite/active. Self-hosted default.
    Otherwise: reads companion_subscriptions, falling back to free if absent.
    """
    if _personal_mode():
        return Subscription(user_id=user_id, tier="elite", status="active")
    try:
        async with get_conn() as conn:
            row = await (
                await conn.execute(
                    "SELECT tier, status, period_start, period_end, "
                    "stripe_customer_id, stripe_subscription_id "
                    "FROM companion_subscriptions WHERE user_id = %s",
                    (user_id,),
                )
            ).fetchone()
        if row:
            return Subscription(
                user_id=user_id,
                tier=row[0],
                status=row[1],
                period_start=row[2],
                period_end=row[3],
                stripe_customer_id=row[4],
                stripe_subscription_id=row[5],
            )
    except Exception as e:
        logger.warning("Subscription lookup failed for %s: %s", user_id, e)
    return Subscription(user_id=user_id, tier="free", status="active")


async def get_tier(user_id: str) -> str:
    """Convenience: just the tier string ('free' / 'pro' / 'elite')."""
    sub = await get_subscription(user_id)
    return sub.tier if sub.is_active else "free"


async def has_feature(user_id: str, feature: str) -> bool:
    """Check whether a feature is enabled for the user's current tier."""
    sub = await get_subscription(user_id)
    if not sub.is_active:
        return bool(TIER_FEATURES["free"].get(feature, False))
    val = sub.features.get(feature)
    return bool(val)


# ── Quota counters ──────────────────────────────────────────────────────────


def _period_key(window: str) -> str:
    """Return today's period key. 'daily' = YYYY-MM-DD, 'monthly' = YYYY-MM."""
    now = datetime.now(timezone.utc)
    if window == "monthly":
        return now.strftime("%Y-%m")
    return now.strftime("%Y-%m-%d")


COUNTER_WINDOWS: dict[str, str] = {
    "chat_messages_per_day": "daily",
    "image_gen_per_day": "daily",
}


async def check_quota(user_id: str, counter: str) -> tuple[int, int | None]:
    """Return (used, limit). limit=None means unlimited.

    Does NOT increment — call consume_quota for the atomic check+increment.
    """
    sub = await get_subscription(user_id)
    limit = sub.features.get(counter)
    window = COUNTER_WINDOWS.get(counter, "daily")
    period = _period_key(window)
    used = 0
    try:
        async with get_conn() as conn:
            row = await (
                await conn.execute(
                    "SELECT count FROM companion_usage_counters "
                    "WHERE user_id = %s AND counter_name = %s AND period_key = %s",
                    (user_id, counter, period),
                )
            ).fetchone()
            if row:
                used = int(row[0])
    except Exception as e:
        logger.warning("Quota check failed: %s", e)
    return used, limit


async def consume_quota(user_id: str, counter: str, amount: int = 1) -> int:
    """Atomically increment + check. Raises QuotaExceeded if over the tier cap.

    Returns remaining quota (or a large int if unlimited).

    Personal mode: bypasses limits entirely, records usage for analytics.
    """
    sub = await get_subscription(user_id)
    limit = sub.features.get(counter)
    if _personal_mode():
        limit = None  # treat as unlimited; still record for telemetry
    window = COUNTER_WINDOWS.get(counter, "daily")
    period = _period_key(window)
    if limit is None:
        # Unlimited tier — still record usage for analytics
        try:
            async with get_conn_autocommit() as conn:
                await conn.execute(
                    "INSERT INTO companion_usage_counters "
                    "(user_id, counter_name, period_key, count, last_used_at) "
                    "VALUES (%s, %s, %s, %s, NOW()) "
                    "ON CONFLICT (user_id, counter_name, period_key) DO UPDATE "
                    "SET count = companion_usage_counters.count + EXCLUDED.count, "
                    "    last_used_at = NOW()",
                    (user_id, counter, period, amount),
                )
        except Exception as e:
            logger.warning("Usage recording (unlimited) failed: %s", e)
        return 10**9  # unlimited sentinel
    # Atomic upsert + return new count
    try:
        async with get_conn_autocommit() as conn:
            row = await (
                await conn.execute(
                    "INSERT INTO companion_usage_counters "
                    "(user_id, counter_name, period_key, count, last_used_at) "
                    "VALUES (%s, %s, %s, %s, NOW()) "
                    "ON CONFLICT (user_id, counter_name, period_key) DO UPDATE "
                    "SET count = companion_usage_counters.count + EXCLUDED.count, "
                    "    last_used_at = NOW() "
                    "RETURNING count",
                    (user_id, counter, period, amount),
                )
            ).fetchone()
            new_count = int(row[0]) if row else amount
    except Exception as e:
        logger.warning("Quota consume failed: %s", e)
        return max(limit - amount, 0)
    if new_count > limit:
        raise QuotaExceeded(counter, sub.tier, limit)
    return max(limit - new_count, 0)


async def get_usage_summary(user_id: str) -> dict[str, Any]:
    """Return a JSON-safe summary: tier, status, counters used vs limit."""
    sub = await get_subscription(user_id)
    out: dict[str, Any] = {
        "tier": sub.tier,
        "status": sub.status,
        "is_active": sub.is_active,
        "period_end": sub.period_end.isoformat() if sub.period_end else None,
        "counters": {},
    }
    for counter in COUNTER_WINDOWS:
        used, limit = await check_quota(user_id, counter)
        out["counters"][counter] = {"used": used, "limit": limit}
    return out


# ── Stripe webhook ──────────────────────────────────────────────────────────


_STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
_STRIPE_TOLERANCE_SECONDS = 300  # 5min — Stripe's recommended




# Event → handler dispatch








# ── Stripe event handlers ───────────────────────────────────────────────────

















# Re-exports from billing_stripe.py — keep public surface stable.
from app.billing_stripe import (  # noqa: E402,F401
    _apply_subscription,
    _cancel_subscription,
    _json_dumps,
    _mark_event_processed,
    _mark_past_due,
    _record_payment,
    _stripe_obj,
    _stripe_tier_from_price,
    _stripe_ts,
    _user_id_from_metadata,
    handle_stripe_event,
    verify_stripe_signature,
)
