"""Stripe webhook handlers + helpers — extracted from app/billing.py.

S+ Phase 2 §6.1 file-size hygiene split. Per absolute global CLAUDE.md
`feedback_no_password_changes.md`: NO automated billing key rotation.

This module owns the Stripe webhook ingestion path. The public surface
(`get_subscription`, `check_quota`, `consume_quota`, etc.) stays in
`app/billing.py` — Stripe event handling moved here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from .db import get_conn_autocommit

logger = logging.getLogger(__name__)

# Stripe webhook config (sole consumer is this module).
_STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
_STRIPE_TOLERANCE_SECONDS = 300  # 5min — Stripe's recommended tolerance window.

_PRICE_TIER_MAP: dict[str, str] = {}

_EVENT_HANDLERS: dict[str, str] = {
    "customer.subscription.created": "_apply_subscription",
    "customer.subscription.updated": "_apply_subscription",
    "customer.subscription.deleted": "_cancel_subscription",
    "invoice.paid": "_record_payment",
    "invoice.payment_failed": "_mark_past_due",
}


def verify_stripe_signature(payload: bytes, sig_header: str, secret: str | None = None) -> bool:
    """Verify Stripe-Signature header per
    https://stripe.com/docs/webhooks/signatures.

    Header format: `t=<ts>,v1=<sig>`. Reject if timestamp older than tolerance
    or HMAC mismatch.
    """
    secret = secret or _STRIPE_WEBHOOK_SECRET
    if not secret:
        return False
    try:
        items = dict(p.split("=", 1) for p in sig_header.split(","))
        ts = int(items["t"])
        expected = items.get("v1", "")
    except Exception:
        return False
    if abs(int(datetime.now(timezone.utc).timestamp()) - ts) > _STRIPE_TOLERANCE_SECONDS:
        return False
    signed_payload = f"{ts}.".encode() + payload
    mac = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, expected)
async def handle_stripe_event(event: dict[str, Any]) -> dict[str, Any]:
    """Process a verified Stripe event. Idempotent via event id PK."""
    event_id = event.get("id", "")
    event_type = event.get("type", "")
    if not event_id:
        return {"ok": False, "reason": "missing event id"}

    # Idempotency check. The row is inserted processed=FALSE BEFORE handling,
    # so a conflict does NOT mean the event was handled — it may be a Stripe
    # retry of an attempt that crashed mid-handler. Only processed=TRUE rows
    # are true replays; processed=FALSE conflicts must be re-dispatched or
    # the event is dropped forever.
    try:
        async with get_conn_autocommit() as conn:
            row = await (
                await conn.execute(
                    "INSERT INTO companion_stripe_events "
                    "(event_id, event_type, payload, processed) "
                    "VALUES (%s, %s, %s::jsonb, FALSE) "
                    "ON CONFLICT (event_id) DO NOTHING "
                    "RETURNING event_id",
                    (event_id, event_type, _json_dumps(event)),
                )
            ).fetchone()
            if not row:
                prior = await (
                    await conn.execute(
                        "SELECT processed FROM companion_stripe_events "
                        "WHERE event_id = %s",
                        (event_id,),
                    )
                ).fetchone()
                if prior is None:
                    # Conflict yet no row found — inconsistent; fail so
                    # Stripe retries rather than silently dropping the event.
                    logger.error(
                        "Stripe idempotency lookup found no row for %s", event_id)
                    return {"ok": False, "reason": "idempotency lookup failed"}
                if prior[0]:
                    return {"ok": True, "replay": True}
                logger.warning(
                    "Stripe event %s retried with processed=FALSE — "
                    "re-dispatching handler", event_id)
    except Exception as e:
        logger.error("Stripe event insert failed: %s", e)
        return {"ok": False, "reason": str(e)}

    handler_name = _EVENT_HANDLERS.get(event_type)
    if not handler_name:
        # Acknowledge unknown event types — mark processed so we don't retry
        await _mark_event_processed(event_id)
        return {"ok": True, "handler": None}

    try:
        handler = globals()[handler_name]
        result = await handler(event)
        await _mark_event_processed(event_id)
        return {"ok": True, "handler": handler_name, "result": result}
    except Exception as e:
        logger.exception("Handler %s failed for %s: %s", handler_name, event_id, e)
        try:
            async with get_conn_autocommit() as conn:
                await conn.execute(
                    "UPDATE companion_stripe_events SET error = %s WHERE event_id = %s",
                    (str(e), event_id),
                )
        except Exception:
            pass
        return {"ok": False, "reason": str(e)}
def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, default=str)
async def _mark_event_processed(event_id: str) -> None:
    try:
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "UPDATE companion_stripe_events SET processed = TRUE WHERE event_id = %s",
                (event_id,),
            )
    except Exception:
        pass
def _stripe_obj(event: dict[str, Any]) -> dict[str, Any]:
    return event.get("data", {}).get("object", {}) or {}
def _stripe_tier_from_price(price_id: str | None) -> str:
    """Map a Stripe price id to our internal tier name."""
    if not price_id:
        return "free"
    pro = (
        os.environ.get("STRIPE_PRICE_PRO_MONTHLY"),
        os.environ.get("STRIPE_PRICE_PRO_ANNUAL"),
    )
    elite = (
        os.environ.get("STRIPE_PRICE_ELITE_MONTHLY"),
        os.environ.get("STRIPE_PRICE_ELITE_ANNUAL"),
    )
    if price_id in pro:
        return "pro"
    if price_id in elite:
        return "elite"
    return "free"
def _user_id_from_metadata(obj: dict[str, Any]) -> str | None:
    """Stripe customer/subscription metadata.user_id is set by the
    checkout-session creator."""
    meta = obj.get("metadata") or {}
    return meta.get("user_id") or None
async def _apply_subscription(event: dict[str, Any]) -> dict[str, Any]:
    obj = _stripe_obj(event)
    user_id = _user_id_from_metadata(obj)
    if not user_id:
        return {"skipped": True, "reason": "no user_id in metadata"}
    items = (obj.get("items") or {}).get("data") or []
    price_id = items[0].get("price", {}).get("id") if items else None
    tier = _stripe_tier_from_price(price_id)
    status = obj.get("status", "active")
    period_start = _stripe_ts(obj.get("current_period_start"))
    period_end = _stripe_ts(obj.get("current_period_end"))
    customer_id = obj.get("customer")
    subscription_id = obj.get("id")
    async with get_conn_autocommit() as conn:
        await conn.execute(
            "INSERT INTO companion_subscriptions "
            "(user_id, tier, status, period_start, period_end, "
            " stripe_customer_id, stripe_subscription_id, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW()) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "  tier = EXCLUDED.tier, "
            "  status = EXCLUDED.status, "
            "  period_start = EXCLUDED.period_start, "
            "  period_end = EXCLUDED.period_end, "
            "  stripe_customer_id = EXCLUDED.stripe_customer_id, "
            "  stripe_subscription_id = EXCLUDED.stripe_subscription_id, "
            "  updated_at = NOW()",
            (user_id, tier, status, period_start, period_end,
             customer_id, subscription_id),
        )
    return {"user_id": user_id, "tier": tier, "status": status}
async def _cancel_subscription(event: dict[str, Any]) -> dict[str, Any]:
    """Downgrade tier to free. SACRED: chat/episodes/affection preserved."""
    obj = _stripe_obj(event)
    user_id = _user_id_from_metadata(obj)
    sub_id = obj.get("id")
    async with get_conn_autocommit() as conn:
        if user_id:
            await conn.execute(
                "UPDATE companion_subscriptions SET tier = 'free', status = 'canceled', "
                "updated_at = NOW() WHERE user_id = %s",
                (user_id,),
            )
        elif sub_id:
            await conn.execute(
                "UPDATE companion_subscriptions SET tier = 'free', status = 'canceled', "
                "updated_at = NOW() WHERE stripe_subscription_id = %s",
                (sub_id,),
            )
    return {"user_id": user_id, "subscription_id": sub_id, "tier": "free"}
async def _record_payment(event: dict[str, Any]) -> dict[str, Any]:
    """invoice.paid — mark sub active. Stripe pushes subscription.updated too,
    so this is mostly informational."""
    obj = _stripe_obj(event)
    return {"invoice": obj.get("id"), "amount_paid": obj.get("amount_paid")}
async def _mark_past_due(event: dict[str, Any]) -> dict[str, Any]:
    obj = _stripe_obj(event)
    sub_id = obj.get("subscription")
    if sub_id:
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "UPDATE companion_subscriptions SET status = 'past_due', "
                "updated_at = NOW() WHERE stripe_subscription_id = %s",
                (sub_id,),
            )
    return {"subscription_id": sub_id, "status": "past_due"}
def _stripe_ts(epoch: Any) -> datetime | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    except Exception:
        return None
