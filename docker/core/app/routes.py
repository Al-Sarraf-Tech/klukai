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
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import memory_archive
from .context import ws, memory, router, affection, proactive, SESSION_ID
from .db import get_pool
from .helpers import (
    fix_narration as _fix_narration,
    strip_actions_for_tts as _strip_actions_for_tts,
)
from .image_gen import generate_image
from .models import SessionState
from .personality import load_personality
from .push import add_subscription, get_vapid_public_key

logger = logging.getLogger(__name__)


# ── Module-level state ─────────────────────────────────────────────────────
_current_costume = "blazing_star"


def register_routes(app: FastAPI) -> None:  # noqa: C901  (route registration)
    """Attach all HTTP endpoints to *app*."""

    # ── Health ──────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health():
        from .db import check_health as db_health
        db = await db_health()
        status = "ok" if db.get("status") == "ok" else "degraded"
        return {
            "status": status,
            "service": "companion-core",
            "version": "0.1.0",
            "database": db,
        }

    # ── Push subscription ──────────────────────────────────────────────────

    @app.get("/api/vapid-key")
    async def vapid_key():
        return {"key": get_vapid_public_key()}

    @app.post("/api/push/subscribe")
    async def push_subscribe(sub: dict):
        add_subscription(sub)
        return {"ok": True}

    # ── Affection state ────────────────────────────────────────────────────

    @app.get("/api/affection")
    async def get_affection():
        state = await affection.get_state()
        return state.model_dump(mode="json")

    # ── TTS proxy ──────────────────────────────────────────────────────────

    @app.post("/api/tts")
    async def api_tts(req: dict):
        """Proxy TTS request to companion-voice and return base64 audio."""
        text = req.get("text", "")
        if not text:
            return JSONResponse({"error": "No text"}, status_code=400)

        voice_url = os.environ.get("VOICE_URL", "http://companion-voice:8301")
        tts_text = _strip_actions_for_tts(text)
        if not tts_text.strip():
            return JSONResponse({"error": "No speakable text"}, status_code=400)
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    f"{voice_url}/tts",
                    json={"text": tts_text[:500], "language": req.get("language", "en")},
                )
                if r.status_code == 200:
                    import base64
                    return {"audio": base64.b64encode(r.content).decode()}
                return JSONResponse({"error": "TTS failed"}, status_code=r.status_code)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=503)

    # ── Image generation ───────────────────────────────────────────────────

    @app.post("/api/generate-image")
    async def api_generate_image(req: dict):
        """Generate an image via ComfyUI and save to memory archive."""
        prompt = req.get("prompt", "")
        if not prompt:
            return JSONResponse({"error": "No prompt"}, status_code=400)

        img_bytes = await generate_image(prompt)
        if img_bytes:
            import base64
            aff_state = await affection.get_state()
            mem_id = await memory_archive.save_image(
                img_bytes, prompt, "api",
                mood="composed", affection_level=aff_state.level,
            )
            return {
                "image": base64.b64encode(img_bytes).decode(),
                "format": "png",
                "memory_id": mem_id,
            }
        return JSONResponse({"error": "Generation failed"}, status_code=500)

    # ── Gift system ────────────────────────────────────────────────────────

    @app.post("/api/gift")
    async def api_gift(req: dict):
        """Send a gift to Klukai. Returns her reaction and affection change."""
        gift_name = req.get("gift", "")
        user_id = req.get("user", "default")
        if not gift_name:
            return JSONResponse({"error": "No gift specified"}, status_code=400)

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

        aff_state = await affection.get_state()
        aff_state.score = max(0, min(100, aff_state.score + bonus))
        await affection._save_state(aff_state)

        reaction = reactions.get(tier, "...Noted.")
        if ws.is_connected(user_id):
            await ws.send_proactive(user_id, reaction)
            await ws.send_affection(user_id, aff_state.score, aff_state.level, aff_state.level_name, bonus)

        return {"tier": tier, "bonus": bonus, "reaction": reaction, "new_score": aff_state.score}

    # ── Mission mode ───────────────────────────────────────────────────────

    @app.post("/api/mission")
    async def api_mission(req: dict):
        """Send Klukai on a mission. She returns with a report and a gift."""
        user_id = req.get("user", "default")

        if ws.is_connected(user_id):
            await ws.send_proactive(user_id, "Understood, Commander. Deploying for sortie. I will report back shortly.")

        aff_state = await affection.get_state()
        asyncio.create_task(_run_mission(user_id, aff_state.level))
        return {"status": "deployed"}

    # ── Milestones ─────────────────────────────────────────────────────────

    @app.get("/api/milestones")
    async def api_milestones():
        """Get all recorded relationship milestones."""
        milestones = await memory.get_milestones()
        return {"milestones": milestones}

    # ── Costume ────────────────────────────────────────────────────────────

    @app.get("/api/costume")
    async def api_get_costume():
        return {"costume": _current_costume}

    @app.post("/api/costume")
    async def api_set_costume(req: dict):
        global _current_costume
        costume = req.get("costume", "blazing_star")
        valid = ["blazing_star", "speed_star", "astral_luminous", "cerulean_breaker"]
        if costume not in valid:
            return JSONResponse({"error": f"Invalid. Choose from: {valid}"}, status_code=400)
        _current_costume = costume
        return {"costume": _current_costume}

    # ── STT proxy ──────────────────────────────────────────────────────────

    @app.post("/api/stt")
    async def api_stt(req: dict):
        """Proxy STT request to companion-voice."""
        audio = req.get("audio", "")
        if not audio:
            return JSONResponse({"error": "No audio"}, status_code=400)
        voice_url = os.environ.get("VOICE_URL", "http://companion-voice:8301")
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(f"{voice_url}/stt", json={"audio": audio})
                return r.json()
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=503)

    # ── Conversation history ───────────────────────────────────────────────

    @app.get("/api/messages")
    async def get_messages(limit: int = 50, before: str | None = None):
        """Fetch recent messages from PostgreSQL."""
        try:
            pool = get_pool()
            async with pool.connection() as conn:
                if before:
                    rows = await conn.execute(
                        "SELECT id, role, content, content_type, mood, model, created_at "
                        "FROM companion_messages WHERE created_at < %s "
                        "ORDER BY created_at DESC LIMIT %s",
                        (before, limit),
                    )
                else:
                    rows = await conn.execute(
                        "SELECT id, role, content, content_type, mood, model, created_at "
                        "FROM companion_messages "
                        "ORDER BY created_at DESC LIMIT %s",
                        (limit,),
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
        category: str | None = None,
        limit: int = 20,
        before: str | None = None,
    ):
        return await memory_archive.list_memories(category=category, limit=limit, before=before)

    @app.get("/api/memories/categories")
    async def api_memory_categories():
        aff = await affection.get_state()
        return await memory_archive.get_categories(aff.level)

    @app.post("/api/memories/backfill-annotations")
    async def api_backfill_annotations():
        """Trigger annotation backfill for memories with NULL/empty annotations."""
        async def _run_backfill():
            try:
                result = await memory_archive.backfill_annotations()
                logger.info("Annotation backfill finished: %s", result)
            except Exception as e:
                logger.error("Annotation backfill task failed: %s", e)

        asyncio.create_task(_run_backfill())
        return {"status": "started", "message": "Annotation backfill running in background."}

    @app.get("/api/memories/{memory_id}/image")
    async def api_memory_image(memory_id: str):
        from fastapi.responses import Response
        data = await memory_archive.get_image_bytes(memory_id, thumbnail=False)
        if data:
            return Response(content=data, media_type="image/png")
        return JSONResponse({"error": "Not found"}, status_code=404)

    @app.get("/api/memories/{memory_id}/thumbnail")
    async def api_memory_thumbnail(memory_id: str):
        from fastapi.responses import Response
        data = await memory_archive.get_image_bytes(memory_id, thumbnail=True)
        if data:
            return Response(content=data, media_type="image/png")
        return JSONResponse({"error": "Not found"}, status_code=404)

    @app.post("/api/memories/{memory_id}/keep")
    async def api_memory_keep(memory_id: str):
        ok = await memory_archive.update_kept(memory_id, kept=True, kept_by="commander")
        return {"ok": ok}

    @app.post("/api/memories/{memory_id}/discard")
    async def api_memory_discard(memory_id: str):
        ok = await memory_archive.update_kept(memory_id, kept=False)
        return {"ok": ok}

    # ── Root redirect ──────────────────────────────────────────────────────

    @app.get("/")
    async def root():
        """Redirect to PWA or show status."""
        from pathlib import Path
        static_dir = Path("/app/static")
        if static_dir.exists() and (static_dir / "index.html").exists():
            from fastapi.responses import RedirectResponse
            return RedirectResponse("/app/")
        return {"status": "companion-core running", "pwa": "not built yet"}


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

        aff = await affection.get_state()
        aff.score = min(100, aff.score + 3)
        await affection._save_state(aff)
    except Exception as e:
        logger.warning("Mission narrative failed: %s", e)
        if ws.is_connected(user_id):
            await ws.send_proactive(user_id, f"Sortie complete. Found {gift}. Take it, Commander.")
