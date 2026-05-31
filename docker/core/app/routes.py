"""HTTP API routes.

All @app.get / @app.post endpoints are registered here via
``register_routes(app)``.  The WebSocket endpoint and message-handling
loop live in chat.py, registered via ``register_websocket(app)``.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pydantic import BaseModel, Field

from . import memory_archive
from .context import ws, router, affection
from .helpers import (
    client_ip,
    fix_narration as _fix_narration,
    strip_actions_for_tts as _strip_actions_for_tts,
)
from .image_gen import generate_image
from .models import SessionState
from .personality import load_personality
from .push import add_subscription, get_vapid_public_key


# ── Request models ────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str

class TTSRequest(BaseModel):
    text: str = Field(min_length=1)
    language: str = "en"

class STTRequest(BaseModel):
    audio: str = Field(min_length=1)

class ImageGenRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)

class GiftRequest(BaseModel):
    gift: str = Field(min_length=1)

class CostumeRequest(BaseModel):
    costume: str

class PushSubscription(BaseModel):
    endpoint: str
    keys: dict

class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=128)

class TributeRequest(BaseModel):
    """Body for POST /api/tribute — Commander's heartfelt message to Klukai."""
    text: str = Field(min_length=20, max_length=1000)
    make_crown_jewel: bool = True


class AccountDeactivateRequest(BaseModel):
    """Body for POST /api/account/deactivate — soft delete. SACRED chat
    data is preserved per CLAUDE.md absolute rule."""
    confirm: str = Field(pattern=r"^DEACTIVATE$")

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

    # ── Authentication ─────────────────────────────────────────────────────

    @app.post("/api/auth/login")
    async def login(req: LoginRequest, request: Request):
        from .auth import authenticate, check_ip_banned
        ip = client_ip(request)
        if await check_ip_banned(ip):
            return JSONResponse({"error": "IP banned"}, status_code=403)
        token = await authenticate(req.username, req.password, ip)
        if token:
            return {"token": token, "user_id": req.username}
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)

    @app.get("/api/auth/verify")
    async def verify_token(request: Request):
        user_id = await _get_user_id(request)
        if user_id:
            return {"user_id": user_id}
        return JSONResponse({"error": "Invalid token"}, status_code=401)

    # ── Health ──────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health():
        """Cached health probe. Sub-ms on cache hit, ~PG+Redis+Qdrant RTT on miss.

        See app/observability/health_cache.py for TTL + refresh semantics.
        SLO: p99 ≤ 30ms (per docs/slos.md).
        """
        from .observability.health_cache import get_cached_health
        return await get_cached_health()

    @app.get("/api/health/live")
    async def health_live():
        """Liveness probe — process-only, no backend pings.

        Returns 200 unless the process itself is broken. K8s-friendly
        liveness convention: liveness failures cause restart, so we
        intentionally don't fail on a backend outage here.
        """
        from .observability.health_cache import get_live_health
        return get_live_health()

    @app.get("/api/health/ready")
    async def health_ready():
        """Readiness probe — full uncached deep check.

        Forces cache refresh; reports unhealthy if any required backend
        is down. K8s-friendly readiness convention: readiness failures
        remove the pod from service rotation without restarting it.
        """
        from .observability.health_cache import get_fresh_health
        result = await get_fresh_health()
        # 503 if backends are down so load balancers / Cloudflare can
        # short-circuit traffic instead of forwarding to a broken core.
        if result.get("status") == "unhealthy":
            from fastapi.responses import JSONResponse
            return JSONResponse(content=result, status_code=503)
        return result


    # ── Subsystem health (S+ feature: loud failure detection) ───────────

    @app.get("/api/health/subsystems")
    async def subsystem_health(request: Request):
        """Deep health check of all external subsystems.

        Returns per-subsystem status so the UI can show degradation clearly
        instead of failures being silent.
        """
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)

        import httpx as _hx

        results = {}

        # Database
        try:
            from .db import check_health as db_health
            db = await db_health()
            results["database"] = {"status": "ok" if db.get("status") == "ok" else "down", **db}
        except Exception:
            results["database"] = {"status": "down"}

        # Redis
        try:
            from .memory import REDIS_URL
            import redis.asyncio as aioredis
            r = aioredis.from_url(REDIS_URL, decode_responses=True)
            await r.ping()
            await r.aclose()
            results["redis"] = {"status": "ok"}
        except Exception:
            results["redis"] = {"status": "down"}

        # Qdrant
        try:
            from .memory import QDRANT_URL
            async with _hx.AsyncClient(timeout=3.0) as c:
                resp = await c.get(f"{QDRANT_URL}/healthz")
                results["qdrant"] = {"status": "ok" if resp.status_code == 200 else "degraded"}
        except Exception:
            results["qdrant"] = {"status": "down"}

        # LM Studio
        try:
            lm_url = os.environ.get("LM_STUDIO_URL", "http://192.168.50.2:1234")
            async with _hx.AsyncClient(timeout=5.0) as c:
                resp = await c.get(f"{lm_url}/v1/models")
                models = [m["id"] for m in resp.json().get("data", [])]
                results["lm_studio"] = {"status": "ok", "models_loaded": len(models), "models": models}
        except Exception:
            results["lm_studio"] = {"status": "down"}

        # ComfyUI
        try:
            comfy_url = os.environ.get("COMFYUI_URL", "http://192.168.50.2:8388")
            async with _hx.AsyncClient(timeout=5.0) as c:
                resp = await c.get(f"{comfy_url}/system_stats")
                stats = resp.json()
                gpu = stats.get("devices", [{}])[0]
                vram_free_gb = round(gpu.get("vram_free", 0) / 1e9, 1)
                results["comfyui"] = {"status": "ok", "gpu": gpu.get("name", "?"), "vram_free_gb": vram_free_gb}
        except Exception:
            results["comfyui"] = {"status": "down"}

        # Embedding service
        try:
            from .memory import INFERENCE_URL
            async with _hx.AsyncClient(timeout=5.0) as c:
                resp = await c.get(f"{INFERENCE_URL}/health")
                results["embeddings"] = {"status": "ok" if resp.status_code == 200 else "degraded"}
        except Exception:
            results["embeddings"] = {"status": "down"}

        # Voice service
        try:
            voice_url = os.environ.get("VOICE_URL", "http://192.168.50.2:8301")
            async with _hx.AsyncClient(timeout=5.0) as c:
                resp = await c.get(f"{voice_url}/health")
                results["voice"] = {"status": "ok" if resp.status_code == 200 else "degraded"}
        except Exception:
            results["voice"] = {"status": "down"}

        # Overall
        statuses = [v["status"] for v in results.values()]
        if all(s == "ok" for s in statuses):
            overall = "ok"
        elif results["database"]["status"] == "down":
            overall = "critical"
        elif any(s == "down" for s in statuses):
            overall = "degraded"
        else:
            overall = "ok"

        return {"status": overall, "subsystems": results}

    # ── Push subscription ──────────────────────────────────────────────────

    @app.get("/api/vapid-key")
    async def vapid_key():
        return {"key": get_vapid_public_key()}

    @app.post("/api/push/subscribe")
    async def push_subscribe(sub: PushSubscription, request: Request):
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Not authenticated"}, status_code=401)
        await add_subscription(user_id, sub.model_dump())
        return {"ok": True}

    # ── Affection state ────────────────────────────────────────────────────

    @app.get("/api/affection")
    async def get_affection(request: Request):
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        state = await affection.get_state(user_id)
        return state.model_dump(mode="json")

    # ── TTS proxy ──────────────────────────────────────────────────────────

    @app.post("/api/tts")
    async def api_tts(req: TTSRequest, request: Request):
        """Proxy TTS request to companion-voice and return base64 audio."""
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)

        voice_url = os.environ.get("VOICE_URL", "http://companion-voice:8301")
        tts_text = _strip_actions_for_tts(req.text)
        if not tts_text.strip():
            return JSONResponse({"error": "No speakable text"}, status_code=400)
        try:
            from .helpers import voice_auth_headers
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    f"{voice_url}/tts",
                    json={"text": tts_text[:500], "language": req.language},
                    headers=voice_auth_headers(),
                )
                if r.status_code == 200:
                    import base64
                    return {"audio": base64.b64encode(r.content).decode()}
                return JSONResponse({"error": "TTS failed"}, status_code=r.status_code)
        except Exception:
            return JSONResponse({"error": "Voice service unavailable"}, status_code=503)

    # ── Image generation ───────────────────────────────────────────────────

    @app.post("/api/generate-image")
    async def api_generate_image(req: ImageGenRequest, request: Request):
        """Generate an image via ComfyUI and save to memory archive."""
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        img_bytes = await generate_image(req.prompt)
        if img_bytes:
            import base64
            aff_state = await affection.get_state(user_id)
            mem_id = await memory_archive.save_image(
                img_bytes, req.prompt, "api",
                mood="composed", affection_level=aff_state.level,
                user_id=user_id,
            )
            return {
                "image": base64.b64encode(img_bytes).decode(),
                "format": "png",
                "memory_id": mem_id,
            }
        return JSONResponse({"error": "Generation failed"}, status_code=500)

    # ── Gift system ────────────────────────────────────────────────────────

    @app.post("/api/gift")
    async def api_gift(req: GiftRequest, request: Request):
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
            await ws.send_affection(user_id, aff_state.score, aff_state.level, aff_state.level_name, bonus)

        try:
            from . import audit
            ip = request.client.host if request.client else None
            await audit.log(
                audit.EVENT_GIFT_GIVEN, user_id=user_id, ip_address=ip,
                request_id=getattr(request.state, "request_id", None),
                metadata={"gift": gift_name, "tier": tier, "bonus": bonus},
            )
        except Exception:
            pass

        return {"tier": tier, "bonus": bonus, "reaction": reaction, "new_score": aff_state.score}
    # Remaining endpoints split for file-size hygiene (S+ Phase 2 §6.1).
    from .routes_extras import register_extras
    from .routes_extras2 import register_extras2
    from .routes_extras3 import register_extras3
    register_extras(app)
    register_extras2(app)
    register_extras3(app)


# ── Private helpers (used by routes above) ─────────────────────────────────


async def _run_mission(user_id: str, affection_level: int) -> None:
    """Background: generate and deliver a mission narrative."""
    import random
    await asyncio.sleep(15)  # Simulate mission duration

    gifts = ["a signal relay component", "a field ration set", "a data chip", "a comm device", "a tactical flashlight"]
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
        async for token in router.stream(prompt, [{"role": "user", "content": prompt}], config):
            full.append(token)
        report = _fix_narration("".join(full)) if full else f"Sortie complete. I found {gift}. Take it."

        if ws.is_connected(user_id):
            await ws.send_proactive(user_id, report)

        # Flat mission reward — clamps to MAX_SCORE and recomputes the level.
        # (Previously capped at 100, which truncated any score above 100.)
        await affection.add_score(3, user_id)
    except Exception as e:
        logger.warning("Mission narrative failed: %s", e)
        if ws.is_connected(user_id):
            await ws.send_proactive(user_id, f"Sortie complete. Found {gift}. Take it, Commander.")
