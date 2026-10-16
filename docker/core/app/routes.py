"""HTTP API routes.

All @app.get / @app.post endpoints are registered here via
``register_routes(app)``.  The WebSocket endpoint and message-handling
loop live in chat.py, registered via ``register_websocket(app)``.
"""

from __future__ import annotations
from typing import Any


import asyncio
import logging

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


from . import memory_archive
from .context import ws, router, affection
from .helpers import (
    client_ip,
    fix_narration as _fix_narration,
    strip_actions_for_tts as _strip_actions_for_tts,
    tts_clip as _tts_clip,
)
from .image_gen import generate_image
from .llm_router import FAILURE_SENTINEL
from .models import SessionState
from .routes_health import register_health_routes
from .personality import load_personality
from .push import add_subscription, get_vapid_public_key
from .passwords import PasswordWorkBusy
from .voice_client import voice_url as _voice_url


# ── Request models ────────────────────────────────────────────────────────


from .api_schemas import (
    LoginRequest as LoginRequest,
    TTSRequest as TTSRequest,
    STTRequest as STTRequest,
    ImageGenRequest as ImageGenRequest,
    GiftRequest as GiftRequest,
    CostumeRequest as CostumeRequest,
    PushSubscription as PushSubscription,
    ChangePasswordRequest as ChangePasswordRequest,
    TributeRequest as TributeRequest,
    AccountDeactivateRequest as AccountDeactivateRequest,
)

logger = logging.getLogger(__name__)


# ── Module-level state ─────────────────────────────────────────────────────
_current_costume = "blazing_star"


async def _get_user_id(request: Request) -> str | None:
    """Extract user_id from Authorization header. Returns None if invalid."""
    from .auth import get_user_from_token

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        return await get_user_from_token(token)
    return None


def register_routes(app: FastAPI) -> None:  # noqa: C901  (route registration)
    """Attach all HTTP endpoints to *app*."""

    from .voice_jobs import VoiceJobs, VoiceQueueFull
    from .voice_client import async_voice_jobs_enabled, local_voice_enabled

    app.state.voice_jobs = VoiceJobs()

    # ── Authentication ─────────────────────────────────────────────────────

    @app.post("/api/auth/login")
    async def login(req: LoginRequest, request: Request) -> Any:
        from .auth import authenticate, check_ip_banned

        ip = client_ip(request)
        if await check_ip_banned(ip):
            return JSONResponse({"error": "IP banned"}, status_code=403)
        try:
            token = await authenticate(req.username, req.password, ip)
        except PasswordWorkBusy:
            return JSONResponse(
                {"error": "Authentication busy; retry shortly"},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        if token:
            return {"token": token, "user_id": req.username}
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)

    @app.get("/api/auth/verify")
    async def verify_token(request: Request) -> Any:
        user_id = await _get_user_id(request)
        if user_id:
            return {"user_id": user_id}
        return JSONResponse({"error": "Invalid token"}, status_code=401)

    register_health_routes(
        app, lambda request: _get_user_id(request), lambda: _voice_url()
    )

    # ── Push subscription ──────────────────────────────────────────────────

    @app.get("/api/vapid-key")
    async def vapid_key() -> Any:
        return {"key": get_vapid_public_key()}

    @app.post("/api/push/subscribe")
    async def push_subscribe(sub: PushSubscription, request: Request) -> Any:
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Not authenticated"}, status_code=401)
        await add_subscription(user_id, sub.model_dump())
        return {"ok": True}

    # ── Affection state ────────────────────────────────────────────────────

    @app.get("/api/affection")
    async def get_affection(request: Request) -> Any:
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        state = await affection.get_state(user_id)
        return state.model_dump(mode="json")

    # ── TTS proxy ──────────────────────────────────────────────────────────

    @app.post("/api/tts")
    async def api_tts(req: TTSRequest, request: Request) -> Any:
        """Proxy TTS request to companion-voice and return base64 audio."""
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)

        tts_text = _strip_actions_for_tts(req.text)
        if not tts_text.strip():
            return JSONResponse({"error": "No speakable text"}, status_code=400)
        if async_voice_jobs_enabled():
            if len(tts_text) > 500:
                return JSONResponse(
                    {"error": "Split speech into chunks of at most 500 characters"},
                    status_code=400,
                )
            if local_voice_enabled() and req.language != "en":
                return JSONResponse(
                    {"error": "This voice currently supports English text"},
                    status_code=400,
                )
            try:
                job = app.state.voice_jobs.submit(
                    user_id, {"text": tts_text, "language": req.language}
                )
                return JSONResponse(
                    job, status_code=202, headers={"Cache-Control": "no-store"}
                )
            except VoiceQueueFull:
                return JSONResponse(
                    {"error": "Voice queue is full"},
                    status_code=429,
                    headers={"Retry-After": "5"},
                )
        try:
            from .voice_client import post_leased_tts

            async with httpx.AsyncClient(
                timeout=60.0, trust_env=False, follow_redirects=False
            ) as client:
                # Bound the WHOLE leased call — gate wait, GPU lease, synth —
                # not just the HTTP leg the client timeout covers.
                r = await asyncio.wait_for(
                    post_leased_tts(
                        client,
                        {"text": _tts_clip(tts_text), "language": req.language},
                        timeout=60.0,
                    ),
                    timeout=45.0,
                )
                if r.status_code == 200:
                    import base64

                    return {"audio": base64.b64encode(r.content).decode()}
                # Never forward the upstream status: a VOICE_API_TOKEN mismatch
                # is a 401 from the voice host, and the stage's net.js reads any
                # 401 as HIS session expiring and logs him out.
                logger.error("TTS upstream HTTP %s", r.status_code)
                return JSONResponse({"error": "TTS failed"}, status_code=502)
        except TimeoutError:
            logger.warning("TTS timed out waiting on the voice GPU lease")
            return JSONResponse({"error": "Voice service unavailable"}, status_code=503)
        except Exception as e:
            # Upstream authentication errors must not expire the user's session.
            logger.error("TTS failed: %s: %s", type(e).__name__, e)
            return JSONResponse({"error": "Voice service unavailable"}, status_code=503)

    @app.get("/api/tts/jobs/{job_id}")
    async def api_tts_job(job_id: str, request: Request) -> Any:
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        try:
            result = app.state.voice_jobs.get(user_id, job_id)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except KeyError:
            return JSONResponse({"error": "Voice job not found"}, status_code=404)

    @app.delete("/api/tts/jobs/{job_id}")
    async def api_tts_cancel(job_id: str, request: Request) -> Any:
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        try:
            result = await app.state.voice_jobs.cancel(user_id, job_id)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except KeyError:
            return JSONResponse({"error": "Voice job not found"}, status_code=404)

    # ── Image generation ───────────────────────────────────────────────────

    @app.post("/api/generate-image")
    async def api_generate_image(req: ImageGenRequest, request: Request) -> Any:
        """Generate an authenticated image and save to this user's archive."""
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        from .agy_image_provider import uses_agy_images
        from .image_provider import selected_provider

        if selected_provider() == "agy" or uses_agy_images(user_id):
            from .account_images import complete_archive, fulfill_account_image
            from .image_gen import is_couple_scene, is_landscape

            width, height = (1216, 832) if is_landscape(req.prompt) else (832, 1216)
            aff_state = await affection.get_state(user_id)
            try:
                outcome = await fulfill_account_image(
                    user_id,
                    req.prompt,
                    width=width,
                    height=height,
                    include_commander=is_couple_scene(req.prompt),
                    request_id=req.request_id,
                    archive_context={
                        "conversation_id": "api",
                        "mood": "composed",
                        "affection_level": aff_state.level,
                    },
                )
            except ValueError:
                return JSONResponse({"error": "Invalid image request"}, status_code=400)
            if outcome.get("needs_archive"):
                metadata = outcome["archive_metadata"]
                mem_id = await memory_archive.save_image(
                    outcome["image"],
                    metadata["prompt"],
                    metadata["conversation_id"],
                    mood=metadata["mood"],
                    affection_level=metadata["affection_level"],
                    user_id=user_id,
                    memory_id=outcome["request_id"],
                )
                outcome = await complete_archive(
                    outcome["request_id"], mem_id, outcome["image"], user_id
                )
            if outcome.get("image"):
                import base64

                body = {
                    "image": base64.b64encode(outcome["image"]).decode(),
                    "format": "png",
                    "generated": True,
                    "archived": bool(outcome.get("archived")),
                    "request_id": outcome["request_id"],
                }
                if outcome.get("archived") and outcome.get("memory_id"):
                    body["memory_id"] = outcome["memory_id"]
                return body
            status = (
                429
                if outcome.get("phase") == "busy"
                else 409
                if outcome.get("phase") in {"conflict", "refused", "pending", "unknown"}
                else 500
            )
            return JSONResponse(
                {
                    "error": "Generation failed",
                    "phase": outcome.get("phase"),
                    "request_id": outcome.get("request_id"),
                    "generated": False,
                    "archived": False,
                },
                status_code=status,
            )
        img_bytes = await generate_image(req.prompt)
        if img_bytes:
            import base64

            aff_state = await affection.get_state(user_id)
            mem_id = await memory_archive.save_image(
                img_bytes,
                req.prompt,
                "api",
                mood="composed",
                affection_level=aff_state.level,
                user_id=user_id,
            )
            return {
                "image": base64.b64encode(img_bytes).decode(),
                "format": "png",
                "memory_id": mem_id,
                "archived": mem_id is not None,
                "generated": True,
            }
        return JSONResponse({"error": "Generation failed"}, status_code=500)

    # ── Gift system ────────────────────────────────────────────────────────

    @app.post("/api/gift")
    async def api_gift(req: GiftRequest, request: Request) -> Any:
        """Send a gift to Klukai. Returns her reaction and affection change."""
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        gift_name = req.gift

        p = load_personality()
        prefs = p.get("gift_preferences", {})
        reactions = p.get("gift_reactions", {})

        if gift_name in prefs.get("loved", []):
            tier, bonus = "loved", 10
        elif gift_name in prefs.get("favoured", []):
            tier, bonus = "favoured", 5
        elif gift_name in prefs.get("liked", []):
            tier, bonus = "liked", 2
        else:
            tier, bonus = "disliked", -1

        # Flat gift bonus — add_score clamps and recomputes the level so the
        # value pushed to the client below reflects the real, current level.
        aff_state = await affection.add_score(bonus, user_id)

        # Record the gift so it counts in stats / "Your Journey" / anniversaries
        # (previously only gifts the LLM detected mid-chat were stored). A disliked
        # gift still bumps affection but isn't kept as a treasured comfort object.
        if tier != "disliked":
            try:
                from .context import proactive

                await proactive.store_gift(user_id, gift_name, sentiment=tier)
                await proactive.record_first(user_id, "first_gift")
            except Exception:
                pass

        reaction = reactions.get(tier, "...Noted.")
        if ws.is_connected(user_id):
            await ws.send_proactive(user_id, reaction)
            await ws.send_affection(
                user_id, aff_state.score, aff_state.level, aff_state.level_name, bonus
            )

        try:
            from . import audit

            ip = request.client.host if request.client else None
            await audit.log(
                audit.EVENT_GIFT_GIVEN,
                user_id=user_id,
                ip_address=ip,
                request_id=getattr(request.state, "request_id", None),
                metadata={"gift": gift_name, "tier": tier, "bonus": bonus},
            )
        except Exception:
            pass

        return {
            "tier": tier,
            "bonus": bonus,
            "reaction": reaction,
            "new_score": aff_state.score,
        }

    # Remaining endpoints split for file-size hygiene (S+ Phase 2 §6.1).
    from .routes_extras import register_extras
    from .routes_extras2 import register_extras2
    from .routes_extras3 import register_extras3
    from .routes_thread import register_thread_routes
    from .routes_wardrobe import register_wardrobe_routes
    from .routes_portrait import register_portrait_routes

    register_extras(app)
    register_extras2(app)
    register_extras3(app)
    register_thread_routes(app)
    register_wardrobe_routes(app)
    register_portrait_routes(app)


# ── Private helpers (used by routes above) ─────────────────────────────────


async def _run_mission(user_id: str, affection_level: int) -> None:
    """Background: generate and deliver a mission narrative."""
    import random

    await asyncio.sleep(15)  # Simulate mission duration

    gifts = [
        "a signal relay component",
        "a field ration set",
        "a data chip",
        "a comm device",
        "a tactical flashlight",
    ]
    gift = random.choice(gifts)

    if affection_level >= 3:
        tone = "Write warmly. You found something special for the Commander."
    elif affection_level >= 1:
        tone = "Write professionally with subtle care."
    else:
        tone = "Write coldly and efficiently."

    prompt = (
        f"You are Klukai writing a 2-3 sentence mission debrief. {tone} "
        f"You completed a patrol in the Yellow Zone. No major hostiles. "
        f"You found {gift} and brought it back for the Commander. Use first person."
    )

    try:
        config = await router.route("mission", SessionState(conversation_id="mission"))
        full = []
        async for token in router.stream(
            prompt, [{"role": "user", "content": prompt}], config
        ):
            full.append(token)
        narrative = "".join(full)
        # A down GPU host streams the sentinel; that is not her debrief, so
        # the canned line stands in rather than an outage notice in her voice.
        report = (
            _fix_narration(narrative)
            if narrative and not narrative.startswith(FAILURE_SENTINEL)
            else f"Sortie complete. I found {gift}. Take it."
        )

        if ws.is_connected(user_id):
            await ws.send_proactive(user_id, report)

        # Flat mission reward — clamps to MAX_SCORE and recomputes the level.
        # (Previously capped at 100, which truncated any score above 100.)
        await affection.add_score(3, user_id)
    except Exception as e:
        logger.warning("Mission narrative failed: %s", e)
        if ws.is_connected(user_id):
            await ws.send_proactive(
                user_id, f"Sortie complete. Found {gift}. Take it, Commander."
            )
