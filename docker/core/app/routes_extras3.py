"""Split route handlers — group 3 from app/routes.py (S+ Phase 2 §6.1)."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .context import affection
from .db import get_pool

from .routes import (
    AccountDeactivateRequest,
)

logger = logging.getLogger(__name__)


async def _get_user_id(request: Request) -> str | None:
    """Local mirror of routes.py:_get_user_id."""
    from .auth import get_user_from_token
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return await get_user_from_token(auth[7:])


def register_extras3(app: FastAPI) -> None:
    """Register group-3 HTTP endpoints."""
    @app.get("/api/user/stats")
    async def api_user_stats(request: Request):
        """Aggregate per-user stats for 'Your Journey' dashboard.

        Counts messages, distinct-day activity, memories kept, gifts given,
        milestones reached, and current affection snapshot.
        """
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        try:
            pool = get_pool()
            async with pool.connection() as conn:
                msg = await (await conn.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT DATE(created_at)), MIN(created_at), MAX(created_at) "
                    "FROM companion_messages WHERE user_id = %s",
                    (user_id,),
                )).fetchone()
                total_messages, days_active, first_msg, last_msg = (msg or (0, 0, None, None))

                user_msg = await (await conn.execute(
                    "SELECT COUNT(*) FROM companion_messages WHERE user_id = %s AND role = 'user'",
                    (user_id,),
                )).fetchone()
                user_message_count = (user_msg or (0,))[0]

                mem = await (await conn.execute(
                    "SELECT COUNT(*), COUNT(*) FILTER (WHERE kept = true), "
                    "COUNT(*) FILTER (WHERE filename IS NOT NULL) "
                    "FROM companion_memories WHERE user_id = %s",
                    (user_id,),
                )).fetchone()
                total_memories, memories_kept, memories_with_image = (mem or (0, 0, 0))

                gift = await (await conn.execute(
                    "SELECT COUNT(*) FROM companion_gifts WHERE user_id = %s",
                    (user_id,),
                )).fetchone()
                gift_count = (gift or (0,))[0]

                first = await (await conn.execute(
                    "SELECT COUNT(*) FROM companion_firsts WHERE user_id = %s",
                    (user_id,),
                )).fetchone()
                firsts_count = (first or (0,))[0]

            aff = await affection.get_state(user_id)
            return {
                "user_id": user_id,
                "total_messages": total_messages,
                "user_messages": user_message_count,
                "klukai_messages": total_messages - user_message_count,
                "days_active": days_active,
                "first_interaction": first_msg.isoformat() if first_msg else None,
                "last_interaction": last_msg.isoformat() if last_msg else None,
                "memories": {
                    "total": total_memories,
                    "kept": memories_kept,
                    "with_image": memories_with_image,
                },
                "gifts_given": gift_count,
                "milestones_reached": firsts_count,
                "affection": {
                    "score": aff.score,
                    "level": aff.level,
                    "level_name": aff.level_name,
                    "consecutive_days": aff.consecutive_days,
                    "total_interactions": aff.total_interactions,
                },
            }
        except Exception as e:
            logger.error("User stats failed: %s", e)
            return JSONResponse({"error": "Stats unavailable"}, status_code=500)

    # ── Conversation export (user data portability) ────────────────────────

    @app.get("/api/user/export")
    async def api_user_export(request: Request, include_memories: bool = True, include_messages: bool = True):
        """Export the user's data as a single JSON bundle.

        Respects scoping: only the authenticated user's own data is included.
        """
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        try:
            pool = get_pool()
            export: dict = {"user_id": user_id, "exported_at": None}
            from datetime import datetime, timezone
            export["exported_at"] = datetime.now(timezone.utc).isoformat()

            async with pool.connection() as conn:
                if include_messages:
                    rows = await conn.execute(
                        "SELECT role, content, content_type, mood, model, created_at "
                        "FROM companion_messages WHERE user_id = %s "
                        "ORDER BY created_at ASC",
                        (user_id,),
                    )
                    export["messages"] = [
                        {
                            "role": r[0],
                            "content": r[1],
                            "content_type": r[2],
                            "mood": r[3],
                            "model": r[4],
                            "created_at": r[5].isoformat() if r[5] else None,
                        }
                        for r in await rows.fetchall()
                    ]

                rows = await conn.execute(
                    "SELECT event_type, event_date, metadata FROM companion_firsts "
                    "WHERE user_id = %s ORDER BY event_date ASC",
                    (user_id,),
                )
                export["milestones"] = [
                    {"event_type": r[0], "event_date": r[1].isoformat() if r[1] else None, "metadata": r[2]}
                    for r in await rows.fetchall()
                ]

                rows = await conn.execute(
                    "SELECT item, description, sentiment, given_date FROM companion_gifts "
                    "WHERE user_id = %s ORDER BY given_date ASC",
                    (user_id,),
                )
                export["gifts"] = [
                    {"item": r[0], "description": r[1], "sentiment": r[2],
                     "given_date": r[3].isoformat() if r[3] else None}
                    for r in await rows.fetchall()
                ]

                if include_memories:
                    rows = await conn.execute(
                        "SELECT annotation, category, scene_tags, prompt, created_at "
                        "FROM companion_memories WHERE user_id = %s AND kept = true "
                        "ORDER BY created_at ASC",
                        (user_id,),
                    )
                    export["memories_kept"] = [
                        {
                            "annotation": r[0],
                            "category": r[1],
                            "scene_tags": r[2],
                            "prompt": r[3],
                            "created_at": r[4].isoformat() if r[4] else None,
                        }
                        for r in await rows.fetchall()
                    ]

            aff = await affection.get_state(user_id)
            export["affection_snapshot"] = {
                "score": aff.score,
                "level": aff.level,
                "level_name": aff.level_name,
                "consecutive_days": aff.consecutive_days,
                "total_interactions": aff.total_interactions,
                "first_interaction": aff.first_interaction.isoformat() if aff.first_interaction else None,
            }

            # Audit the export
            try:
                from . import audit
                ip = request.client.host if request.client else None
                await audit.log(
                    audit.EVENT_EXPORT_REQUESTED,
                    user_id=user_id,
                    ip_address=ip,
                    request_id=getattr(request.state, "request_id", None),
                    metadata={
                        "include_messages": include_messages,
                        "include_memories": include_memories,
                        "message_count": len(export.get("messages", [])),
                        "memory_count": len(export.get("memories_kept", [])),
                    },
                )
            except Exception:
                pass
            return export
        except Exception as e:
            logger.error("Export failed: %s", e)
            return JSONResponse({"error": "Export failed"}, status_code=500)

    # ── Tier scaffold (dormant — no activation surface) ───────────────────
    #
    # The companion_subscriptions table + tier-gating primitives exist for a
    # potential future paywall. In personal-use mode (default), every user is
    # elite and these endpoints simply report state — they do NOT charge
    # anyone, link to Stripe, or expose any "Subscribe" action.
    #
    # To flip on monetization later:
    #   1. set KLUKAI_PERSONAL_MODE=false
    #   2. restore checkout + portal endpoints (see ADR-0017 history)
    #   3. configure STRIPE_API_KEY, STRIPE_WEBHOOK_SECRET, price IDs

    @app.get("/api/billing/tiers")
    async def billing_tiers():
        """Static tier feature matrix. Public — no monetization tied to it."""
        from .billing import TIER_FEATURES
        return {"features": TIER_FEATURES, "mode": "personal"}

    @app.get("/api/billing/subscription")
    async def get_my_subscription(request: Request):
        from .billing import get_subscription
        user_id = await _get_user_id(request)
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
    async def get_my_usage(request: Request):
        from .billing import get_usage_summary
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await get_usage_summary(user_id)

    @app.post("/api/billing/webhook")
    async def stripe_webhook(request: Request):
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
        return await handle_stripe_event(event)

    # ── Account self-service ───────────────────────────────────────────────

    @app.post("/api/account/deactivate")
    async def deactivate_account(req: AccountDeactivateRequest, request: Request):
        """Soft-delete user account. ABSOLUTE: chat memories, episodes,
        affection, Qdrant vectors are NEVER touched (CLAUDE.md SACRED rule).

        Effect:
        - Account row marked deactivated_at = NOW()
        - All active sessions invalidated (forces re-login if reactivated)
        - Subscription canceled at period end (if Stripe-managed)
        - User can reactivate within 30 days; after that, ops can hard-delete
          the *account row only* with explicit admin action.
        """
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            from .db import get_conn_autocommit
            async with get_conn_autocommit() as conn:
                # deactivated_at column is created by migration 130 — no DDL in
                # the request path (it took an ACCESS EXCLUSIVE lock per call).
                await conn.execute(
                    "UPDATE companion_users SET deactivated_at = NOW() WHERE id = %s",
                    (user_id,),
                )
                # Invalidate all sessions for this user. Tokens live in
                # companion_auth_sessions — companion_sessions never existed, so
                # the old DELETE silently no-op'd and never logged the user out.
                await conn.execute(
                    "DELETE FROM companion_auth_sessions WHERE user_id = %s",
                    (user_id,),
                )
            try:
                from . import audit
                await audit.log(
                    event_type="account.deactivated",
                    user_id=user_id,
                    metadata={"sacred_chat_preserved": True},
                )
            except Exception:
                pass
            return {
                "deactivated": True,
                "user_id": user_id,
                "message": "Account deactivated. Memories preserved. Email support to reactivate.",
            }
        except Exception as e:
            logger.error("Deactivate failed for %s: %s", user_id, e)
            return JSONResponse({"error": "Deactivation failed"}, status_code=500)

    # ── Root redirect ──────────────────────────────────────────────────────

    @app.get("/flutter_service_worker.js")
    async def root_service_worker():
        """Serve self-destructing SW at root scope to kill the old cached SW.

        The Flutter app used to be served at / with a SW at / scope. Now it's
        at /app/ with a SW at /app/ scope. Browsers that still have the old SW
        cached will check /flutter_service_worker.js for updates. This serves
        a version that immediately unregisters itself and clears all caches.
        """
        from fastapi.responses import Response
        sw_code = (
            "self.addEventListener('install', () => self.skipWaiting());\n"
            "self.addEventListener('activate', (e) => {\n"
            "  e.waitUntil(caches.keys()"
            ".then(ns => Promise.all(ns.map(n => caches.delete(n))))"
            ".then(() => self.registration.unregister())"
            ".then(() => self.clients.matchAll({type:'window'}))"
            ".then(cs => cs.forEach(c => c.navigate(c.url))));\n"
            "});\n"
        )
        return Response(
            content=sw_code,
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/")
    async def root():
        """Serve login page or redirect to app."""
        from pathlib import Path
        from fastapi.responses import FileResponse

        login_path = Path("/app/static/login.html")
        if login_path.exists():
            return FileResponse(login_path, media_type="text/html")
        return {"status": "companion-core running", "auth": "login page not deployed"}
