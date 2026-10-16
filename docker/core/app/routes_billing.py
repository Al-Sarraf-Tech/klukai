"""Billing status and authenticated account usage; no account mutation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register_billing_routes(
    app: FastAPI, resolve_user: Callable[[Request], Awaitable[str | None]]
) -> None:
    @app.get("/api/billing/tiers")
    async def billing_tiers() -> Any:
        """Static tier feature matrix. Public — no monetization tied to it."""
        from .billing import TIER_FEATURES

        return {"features": TIER_FEATURES, "mode": "personal"}

    @app.get("/api/billing/subscription")
    async def get_my_subscription(request: Request) -> Any:
        from .billing import get_subscription

        user_id = await resolve_user(request)
        if not user_id:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        sub = await get_subscription(user_id)
        return {
            "tier": sub.tier,
            "status": sub.status,
            "is_active": sub.is_active,
            "period_start": sub.period_start.isoformat() if sub.period_start else None,
            "period_end": sub.period_end.isoformat() if sub.period_end else None,
            "features": sub.features,
        }

    @app.get("/api/billing/usage")
    async def get_my_usage(request: Request) -> Any:
        from .billing import get_usage_summary

        user_id = await resolve_user(request)
        if not user_id:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await get_usage_summary(user_id)

    @app.post("/api/billing/webhook")
    async def stripe_webhook(request: Request) -> Any:
        """Stripe webhook receiver. Kept for future use — verifies signature
        and records the event, but with no STRIPE_WEBHOOK_SECRET configured
        all calls return 400 (signature invalid). Safe no-op in personal mode.
        """
        from .billing import handle_stripe_event, verify_stripe_signature

        body = await request.body()
        sig = request.headers.get("Stripe-Signature", "")
        if not verify_stripe_signature(body, sig):
            return JSONResponse({"error": "Invalid signature"}, status_code=400)
        try:
            import json

            event = json.loads(body)
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        result = await handle_stripe_event(event)
        if not result.get("ok"):
            # 500 → Stripe retries with backoff; a 200 here would silently
            # drop the event forever even though the handler failed.
            return JSONResponse(result, status_code=500)
        return result
