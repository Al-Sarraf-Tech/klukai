"""Split route handlers — group 1 from app/routes.py (S+ Phase 2 §6.1)."""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import memory_archive
from .context import ws, memory, affection
from .auth import is_admin
from .db import get_pool

from .routes import (
    ChangePasswordRequest,
    CostumeRequest,
    STTRequest,
    _run_mission,
)

logger = logging.getLogger(__name__)


def _img_media_type(data: bytes) -> str:
    """Sniff image content-type from magic bytes. Thumbnails are WebP now; full
    images are PNG; older thumbnails may still be PNG/JPEG until regenerated."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return "image/png"


async def _get_user_id(request: Request) -> str | None:
    """Local mirror of routes.py:_get_user_id."""
    from .auth import get_user_from_token
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return await get_user_from_token(auth[7:])


def register_extras(app: FastAPI) -> None:
    """Register group-1 HTTP endpoints."""
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
        # Persisted per-user in the fact store so the choice survives restarts
        # (was a single in-process global that reset on every redeploy).
        costume = await memory.recall_fact("costume", user_id=user_id)
        return {"costume": costume or "blazing_star"}

    @app.post("/api/costume")
    async def api_set_costume(req: CostumeRequest, request: Request):
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        valid = ["blazing_star", "speed_star", "astral_luminous", "cerulean_breaker"]
        if req.costume not in valid:
            return JSONResponse({"error": f"Invalid. Choose from: {valid}"}, status_code=400)
        await memory.store_fact("costume", req.costume, user_id=user_id)
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
        return {"costume": req.costume}

    # ── STT proxy ──────────────────────────────────────────────────────────

    @app.post("/api/stt")
    async def api_stt(req: STTRequest, request: Request):
        """Proxy STT request to companion-voice."""
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        voice_url = os.environ.get("VOICE_URL", "http://companion-voice:8301")
        try:
            from .helpers import voice_auth_headers
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(f"{voice_url}/stt", json={"audio": req.audio}, headers=voice_auth_headers())
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
            return Response(
                content=data, media_type=_img_media_type(data),
                # Images are immutable (content-hash filenames) — let the browser
                # cache them so an album view fetches each once, not on every scroll.
                headers={"Cache-Control": "private, max-age=604800, immutable"},
            )
        return JSONResponse({"error": "Not found"}, status_code=404)

    @app.get("/api/memories/{memory_id}/thumbnail")
    async def api_memory_thumbnail(memory_id: str, request: Request):
        from fastapi.responses import Response
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        data = await memory_archive.get_image_bytes(memory_id, thumbnail=True, user_id=user_id)
        if data:
            return Response(
                content=data, media_type=_img_media_type(data),
                # Images are immutable (content-hash filenames) — let the browser
                # cache them so an album view fetches each once, not on every scroll.
                headers={"Cache-Control": "private, max-age=604800, immutable"},
            )
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
        if not is_admin(user_id):
            return ec.admin_only()
        from .rate_limit import reset, LIMITS
        if bucket not in LIMITS and bucket != "default":
            return ec.err(ec.INPUT_INVALID, f"Unknown bucket: {bucket}", status_code=400,
                           extra={"known": list(LIMITS.keys())})
        await reset(user_id_target, bucket)
        return {"ok": True, "user_id": user_id_target, "bucket": bucket}
