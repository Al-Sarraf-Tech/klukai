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
from .context import ws, memory, router, affection
from .db import get_pool
from .helpers import (
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
        ip = request.client.host if request.client else "unknown"
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
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    f"{voice_url}/tts",
                    json={"text": tts_text[:500], "language": req.language},
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

        aff_state = await affection.get_state(user_id)
        aff_state.score = max(0, min(1000, aff_state.score + bonus))
        await affection._save_state(aff_state, user_id)

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

    # ── Mission mode ───────────────────────────────────────────────────────

    @app.post("/api/mission")
    async def api_mission(request: Request):
        """Send Klukai on a mission. She returns with a report and a gift."""
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)

        if ws.is_connected(user_id):
            await ws.send_proactive(user_id, "Understood, Commander. Deploying for sortie. I will report back shortly.")

        aff_state = await affection.get_state(user_id)
        asyncio.create_task(_run_mission(user_id, aff_state.level))

        try:
            from . import audit
            ip = request.client.host if request.client else None
            await audit.log(
                audit.EVENT_MISSION_STARTED, user_id=user_id, ip_address=ip,
                request_id=getattr(request.state, "request_id", None),
            )
        except Exception:
            pass

        return {"status": "deployed"}

    # ── Milestones ─────────────────────────────────────────────────────────

    @app.get("/api/milestones")
    async def api_milestones(request: Request):
        """Get all recorded relationship milestones for the authenticated user."""
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        milestones = await memory.get_milestones(user_id=user_id)
        return {"milestones": milestones}

    # ── Costume ────────────────────────────────────────────────────────────

    @app.get("/api/costume")
    async def api_get_costume(request: Request):
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        return {"costume": _current_costume}

    @app.post("/api/costume")
    async def api_set_costume(req: CostumeRequest, request: Request):
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        global _current_costume
        valid = ["blazing_star", "speed_star", "astral_luminous", "cerulean_breaker"]
        if req.costume not in valid:
            return JSONResponse({"error": f"Invalid. Choose from: {valid}"}, status_code=400)
        _current_costume = req.costume
        try:
            from . import audit
            ip = request.client.host if request.client else None
            await audit.log(
                audit.EVENT_COSTUME_CHANGED, user_id=user_id, ip_address=ip,
                request_id=getattr(request.state, "request_id", None),
                metadata={"costume": req.costume},
            )
        except Exception:
            pass
        return {"costume": _current_costume}

    # ── STT proxy ──────────────────────────────────────────────────────────

    @app.post("/api/stt")
    async def api_stt(req: STTRequest, request: Request):
        """Proxy STT request to companion-voice."""
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        voice_url = os.environ.get("VOICE_URL", "http://companion-voice:8301")
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(f"{voice_url}/stt", json={"audio": req.audio})
                return r.json()
        except Exception:
            return JSONResponse({"error": "Voice service unavailable"}, status_code=503)

    # ── Conversation history ───────────────────────────────────────────────

    @app.get("/api/messages")
    async def get_messages(request: Request, limit: int = 50, before: str | None = None):
        """Fetch recent messages from PostgreSQL, scoped to authenticated user."""
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        try:
            pool = get_pool()
            async with pool.connection() as conn:
                if before:
                    rows = await conn.execute(
                        "SELECT id, role, content, content_type, mood, model, created_at "
                        "FROM companion_messages WHERE user_id = %s AND created_at < %s "
                        "ORDER BY created_at DESC LIMIT %s",
                        (user_id, before, limit),
                    )
                else:
                    rows = await conn.execute(
                        "SELECT id, role, content, content_type, mood, model, created_at "
                        "FROM companion_messages WHERE user_id = %s "
                        "ORDER BY created_at DESC LIMIT %s",
                        (user_id, limit),
                    )
                messages = [
                    {
                        "id": str(r[0]),
                        "role": r[1],
                        "content": r[2],
                        "content_type": r[3],
                        "mood": r[4],
                        "model": r[5],
                        "created_at": r[6].isoformat(),
                    }
                    for r in await rows.fetchall()
                ]
            messages.reverse()
            return {"messages": messages}
        except Exception as e:
            logger.error("Failed to fetch messages: %s", e)
            return {"messages": []}

    # ── Memory archive API ─────────────────────────────────────────────────
    # NOTE: /api/memories/categories MUST be defined before /api/memories/{memory_id}
    # so FastAPI does not try to parse "categories" as a memory_id path parameter.

    @app.get("/api/memories")
    async def api_memories(
        request: Request,
        category: str | None = None,
        limit: int = 20,
        before: str | None = None,
        month: str | None = None,
    ):
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        return await memory_archive.list_memories(
            category=category, limit=limit, before=before, month=month, user_id=user_id
        )

    @app.get("/api/memories/categories")
    async def api_memory_categories(request: Request):
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        aff = await affection.get_state(user_id)
        return await memory_archive.get_categories(aff.level, user_id=user_id)

    @app.get("/api/memories/timeline")
    async def api_memory_timeline(request: Request):
        """Get month/year groups with memory counts for the archive timeline."""
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        return await memory_archive.get_timeline(user_id=user_id)

    @app.post("/api/memories/backfill-annotations")
    async def api_backfill_annotations(request: Request):
        """Trigger annotation backfill for memories with NULL/empty annotations."""
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)

        async def _run_backfill():
            try:
                result = await memory_archive.backfill_annotations(user_id=user_id)
                logger.info("Annotation backfill finished for %s: %s", user_id, result)
            except Exception as e:
                logger.error("Annotation backfill task failed: %s", e)

        asyncio.create_task(_run_backfill())
        return {"status": "started", "message": "Annotation backfill running in background."}

    @app.get("/api/memories/{memory_id}/image")
    async def api_memory_image(memory_id: str, request: Request):
        from fastapi.responses import Response
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        data = await memory_archive.get_image_bytes(memory_id, thumbnail=False, user_id=user_id)
        if data:
            return Response(content=data, media_type="image/png")
        return JSONResponse({"error": "Not found"}, status_code=404)

    @app.get("/api/memories/{memory_id}/thumbnail")
    async def api_memory_thumbnail(memory_id: str, request: Request):
        from fastapi.responses import Response
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        data = await memory_archive.get_image_bytes(memory_id, thumbnail=True, user_id=user_id)
        if data:
            return Response(content=data, media_type="image/png")
        return JSONResponse({"error": "Not found"}, status_code=404)

    @app.post("/api/memories/{memory_id}/keep")
    async def api_memory_keep(memory_id: str, request: Request):
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        ok = await memory_archive.update_kept(memory_id, kept=True, kept_by="commander", user_id=user_id)
        return {"ok": ok}

    @app.post("/api/memories/{memory_id}/discard")
    async def api_memory_discard(memory_id: str, request: Request):
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        ok = await memory_archive.update_kept(memory_id, kept=False, user_id=user_id)
        return {"ok": ok}

    # ── Session info + password change ──────────────────────────────────────

    @app.get("/api/session/info")
    async def api_session_info(request: Request):
        """Return the current session's expiry + age metadata."""
        from . import error_codes as ec
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return ec.auth_required()
        token = auth[7:]
        from .auth import get_session_info
        info = await get_session_info(token)
        if not info:
            return ec.err(ec.AUTH_INVALID, "Session not found", status_code=401)
        return info

    @app.post("/api/user/change-password")
    async def api_change_password(req: ChangePasswordRequest, request: Request):
        """Change the authenticated user's password.

        Rate-limited via middleware (login bucket — shares brute-force protection).
        Invalidates all existing sessions on success.
        """
        from . import error_codes as ec
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        from .auth import change_password
        ok = await change_password(user_id, req.old_password, req.new_password)
        if not ok:
            return ec.err(ec.AUTH_INVALID, "Current password incorrect or new password too weak",
                           status_code=400)
        return {"ok": True, "sessions_invalidated": True}

    # ── Admin: rate-limit reset ─────────────────────────────────────────────

    @app.post("/api/admin/rate-limit/reset")
    async def api_admin_ratelimit_reset(request: Request, user_id_target: str, bucket: str):
        """Clear a (user_id, bucket) rate-limit counter. Admin only.

        Use for debugging or unstucking a user who hit a limit accidentally.
        """
        from . import error_codes as ec
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        if user_id != "jalsarraf":
            return ec.admin_only()
        from .rate_limit import reset, LIMITS
        if bucket not in LIMITS and bucket != "default":
            return ec.err(ec.INPUT_INVALID, f"Unknown bucket: {bucket}", status_code=400,
                           extra={"known": list(LIMITS.keys())})
        await reset(user_id_target, bucket)
        return {"ok": True, "user_id": user_id_target, "bucket": bucket}

    # ── Tribute system (the "treat her like a princess" feature) ────────────
    # Commander honors Klukai with a heartfelt message. Each tribute is sacred
    # (per feedback_never_delete_chat.md). One tribute per user can be the
    # "crown jewel" — always referenced in her system prompt at affection 4+.
    # 24h cooldown between tributes so they stay rare and meaningful.

    @app.post("/api/tribute")
    async def api_tribute(req: TributeRequest, request: Request):
        """Commander honors Klukai with a heartfelt message.

        Persists as a sacred tribute (never deleted). Bumps affection +20.
        Pushes an elevated-mood proactive response. Optionally promotes to
        crown jewel (always referenced in system prompt at affection 4+).

        Cooldown: 24h between tributes per user.
        """
        from . import error_codes as ec
        from . import tributes
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()

        # 24h cooldown check
        recent = await tributes.count_recent(user_id)
        allowed, reason = tributes.can_send_tribute(recent)
        if not allowed:
            return ec.err(ec.INPUT_INVALID, reason or "Cooldown active",
                          status_code=429,
                          extra={"cooldown_hours": tributes.TRIBUTE_COOLDOWN_HOURS})

        # Capture state at write-time
        aff_state = await affection.get_state(user_id)

        tribute_id = await tributes.save_tribute(
            user_id=user_id,
            text=req.text,
            mood_at_time="grateful",
            affection_at_time=aff_state.score,
            make_crown_jewel=req.make_crown_jewel,
        )
        if not tribute_id:
            return ec.err(ec.INTERNAL_ERROR, "Tribute could not be saved", status_code=500)

        # Bump affection — larger than any single gift
        new_score = min(1000, aff_state.score + tributes.TRIBUTE_AFFECTION_BUMP)
        aff_state.score = new_score
        await affection._save_state(aff_state, user_id)

        # Push elevated-mood response via WS if Commander is connected
        if ws.is_connected(user_id):
            # Klukai's mood lifts to "grateful" — the elevated-vulnerable mood
            # most appropriate for receiving a tribute. The actual response
            # the LLM generates on next turn will reflect this mood.
            try:
                await ws.send_proactive(
                    user_id,
                    "...Commander. (I take a moment, looking down, then back at you.) I... thank you.",
                )
                await ws.send_affection(
                    user_id, aff_state.score, aff_state.level, aff_state.level_name,
                    tributes.TRIBUTE_AFFECTION_BUMP,
                )
            except Exception:
                pass  # Don't fail the tribute write on WS issues

        # Audit the tribute — a sacred record
        try:
            from . import audit
            ip = request.client.host if request.client else None
            await audit.log(
                "tribute_given",
                user_id=user_id,
                ip_address=ip,
                request_id=getattr(request.state, "request_id", None),
                metadata={
                    "tribute_id": tribute_id,
                    "text_length": len(req.text),
                    "is_crown_jewel": req.make_crown_jewel,
                    "affection_bump": tributes.TRIBUTE_AFFECTION_BUMP,
                    "new_score": new_score,
                },
            )
        except Exception:
            pass

        return {
            "ok": True,
            "tribute_id": tribute_id,
            "is_crown_jewel": req.make_crown_jewel,
            "affection_bump": tributes.TRIBUTE_AFFECTION_BUMP,
            "new_score": new_score,
            "mood_shift": "grateful",
        }

    @app.get("/api/tributes")
    async def api_list_tributes(request: Request, limit: int = 20):
        """List the Commander's tributes, newest first."""
        from . import error_codes as ec
        from . import tributes
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        limit = max(1, min(limit, 100))
        items = await tributes.list_tributes(user_id, limit=limit)
        return {"count": len(items), "tributes": items}

    @app.get("/api/tribute/crown")
    async def api_get_crown_jewel(request: Request):
        """Return the current crown-jewel tribute (or null if none set)."""
        from . import error_codes as ec
        from . import tributes
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        crown = await tributes.get_crown_jewel(user_id)
        return {"crown_jewel": crown}

    @app.post("/api/tributes/{tribute_id}/crown")
    async def api_set_crown_jewel(tribute_id: str, request: Request):
        """Promote a tribute to crown jewel (demotes any prior one)."""
        from . import error_codes as ec
        from . import tributes
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        ok = await tributes.set_crown_jewel(user_id, tribute_id)
        if not ok:
            return ec.err(ec.INPUT_INVALID, "Tribute not found", status_code=404)
        return {"ok": True, "crown_jewel_id": tribute_id}

    # ── Dream diary (text-only memories from reflection-on-return) ──────────

    @app.get("/api/dreams")
    async def api_dreams(request: Request, limit: int = 20):
        """List the user's saved dreams (reflection-on-return 'dream' path).

        Dreams are memory-archive rows with category='Dreams', text-only by
        default (filename starts with 'dream-' sentinel). Returns newest-first.
        """
        from . import error_codes as ec
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        from . import dreams as dream_mod
        items = await dream_mod.list_dreams(user_id=user_id, limit=limit)
        total = await dream_mod.count_dreams(user_id=user_id)
        return {"count": len(items), "total": total, "dreams": items}

    # ── Affection timeline (Your Journey graph data) ────────────────────────

    @app.get("/api/user/affection-timeline")
    async def api_affection_timeline(request: Request, days: int = 30):
        """Return the authenticated user's affection-score timeline over N days.

        Pulls from companion_affection_log, bucketed by day. Useful for
        graphing relationship progression in the Flutter UI.
        """
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        days = max(1, min(days, 365))
        try:
            pool = get_pool()
            async with pool.connection() as conn:
                rows = await (await conn.execute(
                    "SELECT DATE(created_at) AS day, "
                    "  MAX(new_score) AS end_score, "
                    "  SUM(delta) AS net_delta, "
                    "  COUNT(*) AS events "
                    "FROM companion_affection_log "
                    "WHERE user_id = %s AND created_at > NOW() - INTERVAL '%s days' "
                    "GROUP BY DATE(created_at) "
                    "ORDER BY day ASC",
                    (user_id, days),
                )).fetchall()
            points = [
                {
                    "date": r[0].isoformat() if r[0] else None,
                    "end_score": r[1],
                    "net_delta": r[2],
                    "events": r[3],
                }
                for r in rows
            ]
            return {"days": days, "count": len(points), "points": points}
        except Exception as e:
            logger.error("Affection timeline failed: %s", e)
            return JSONResponse({"error": "Timeline unavailable"}, status_code=500)

    # ── Audit chain integrity check (admin-only) ──────────────────────────

    @app.get("/api/audit/verify-chain")
    async def api_audit_verify_chain(request: Request, limit: int = 500):
        """Verify the HMAC hash chain over the last N audit rows.

        Admin-only. Returns {valid, break_at_id, checked}. A break means
        a row was modified or deleted since insert.
        """
        from . import error_codes as ec
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        if user_id != "jalsarraf":
            return ec.admin_only()
        limit = max(1, min(limit, 5000))
        try:
            pool = get_pool()
            async with pool.connection() as conn:
                rows_raw = await (await conn.execute(
                    "SELECT id, event_type, user_id, ip_address, request_id, "
                    "metadata, created_at, chain_hash "
                    "FROM companion_audit_log ORDER BY id ASC LIMIT %s",
                    (limit,),
                )).fetchall()
            rows = [
                {
                    "id": r[0], "event_type": r[1], "user_id": r[2],
                    "ip_address": r[3], "request_id": r[4], "metadata": r[5],
                    "created_at": str(r[6] or ""), "chain_hash": r[7],
                }
                for r in rows_raw
            ]
            from . import audit_chain
            return audit_chain.verify_chain(rows)
        except Exception as e:
            logger.error("Audit chain verify failed: %s", e)
            return ec.err(ec.INTERNAL_ERROR, "Verify failed", status_code=500)

    # ── Audit log viewer (admin-only) ──────────────────────────────────────

    @app.get("/api/audit")
    async def api_audit(
        request: Request,
        limit: int = 100,
        event_type: str | None = None,
        user_id_filter: str | None = None,
    ):
        """Read recent audit events. Admin only.

        Query params: limit (1-1000), event_type, user_id_filter.
        """
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        if user_id != "jalsarraf":
            return JSONResponse({"error": "Admin only"}, status_code=403)
        from . import audit
        events = await audit.recent(limit=limit, event_type=event_type,
                                    user_id=user_id_filter)
        return {"count": len(events), "events": events}

    # ── Metrics (admin-only) ────────────────────────────────────────────────

    @app.get("/api/metrics")
    async def api_metrics(request: Request):
        """In-process metrics snapshot (admin user only).

        Returns counters, latency histograms, and uptime. Admin scoping:
        user must be 'jalsarraf' (the operator). Other users get 403.
        """
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        if user_id != "jalsarraf":
            return JSONResponse({"error": "Admin only"}, status_code=403)
        from . import metrics as _m
        snap = _m.snapshot()
        try:
            pool = get_pool()
            snap["db_pool"] = {
                "min_size": pool.min_size,
                "max_size": pool.max_size,
            }
        except Exception:
            snap["db_pool"] = {"status": "unavailable"}
        return snap

    # ── Memory search (full-text over annotations + prompts) ───────────────

    @app.get("/api/memories/search")
    async def api_memories_search(request: Request, q: str, limit: int = 20):
        """Search memory annotations by ILIKE substring. Scoped to user."""
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        if not q or len(q.strip()) < 2:
            return JSONResponse({"error": "Query must be at least 2 characters"}, status_code=400)
        limit = max(1, min(limit, 100))
        try:
            pool = get_pool()
            async with pool.connection() as conn:
                rows = await conn.execute(
                    "SELECT id, filename, annotation, category, scene_tags, created_at "
                    "FROM companion_memories "
                    "WHERE user_id = %s "
                    "  AND (annotation ILIKE %s OR prompt ILIKE %s) "
                    "  AND kept = true "
                    "ORDER BY created_at DESC LIMIT %s",
                    (user_id, f"%{q}%", f"%{q}%", limit),
                )
                results = [
                    {
                        "id": str(r[0]),
                        "filename": r[1],
                        "annotation": r[2],
                        "category": r[3],
                        "scene_tags": r[4],
                        "created_at": r[5].isoformat() if r[5] else None,
                    }
                    for r in await rows.fetchall()
                ]
            return {"query": q, "count": len(results), "results": results}
        except Exception as e:
            logger.error("Memory search failed: %s", e)
            return JSONResponse({"error": "Search failed"}, status_code=500)

    # ── User stats (Your Journey) ──────────────────────────────────────────

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
                # Add deactivated_at column lazily (idempotent)
                await conn.execute(
                    "ALTER TABLE companion_users "
                    "ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMPTZ"
                )
                await conn.execute(
                    "UPDATE companion_users SET deactivated_at = NOW() WHERE id = %s",
                    (user_id,),
                )
                # Invalidate all sessions for this user
                await conn.execute(
                    "DELETE FROM companion_sessions WHERE user_id = %s",
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

        aff = await affection.get_state(user_id)
        aff.score = min(100, aff.score + 3)
        await affection._save_state(aff, user_id)
    except Exception as e:
        logger.warning("Mission narrative failed: %s", e)
        if ws.is_connected(user_id):
            await ws.send_proactive(user_id, f"Sortie complete. Found {gift}. Take it, Commander.")
