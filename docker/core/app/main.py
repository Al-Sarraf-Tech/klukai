"""Companion Core: FastAPI application with WebSocket, memory, and LLM routing."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .affection import AffectionManager
from .agent_loop import AgentLoop
from .db import init_pool, close_pool, get_pool, get_conn, get_conn_autocommit
from .image_gen import generate_image, needs_image, build_prompt, is_couple_scene, is_landscape, SQUAD_KEYWORDS, SITUATION_KEYWORDS
from .fact_extractor import create_episode_summary, extract_facts
from .llm_router import LLMRouter
from .mcp_client import MCPClient
from . import memory_archive
from .memory import MemoryManager
from .models import SessionState, new_id
from .personality import assemble_system_prompt, load_personality
from .events import init as events_init, close as events_close
from .proactive import ProactiveEngine
from .push import add_subscription, get_vapid_public_key, send_push
from .ws_manager import WSManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ── Globals ──────────────────────────────────────────────────────────────────

memory = MemoryManager()
router = LLMRouter()
mcp = MCPClient()
ws = WSManager()
proactive = ProactiveEngine()
affection = AffectionManager()

SESSION_ID = "default"  # Single-user, single session

# Tracks the most recently generated memory_id for commander save/discard overrides
_last_memory_id: str | None = None

RECALL_KEYWORDS = [
    "show me a memory", "remember when", "that time we", "do you remember",
    "show me something", "recall a memory", "our memories", "your memories",
]

SAVE_KEYWORDS = ["save that", "keep this", "keep that", "save this"]
DISCARD_KEYWORDS = ["delete that", "remove this", "discard that", "forget that"]

# Trivial messages that don't need fact extraction or affection classification
TRIVIAL_PATTERNS = {
    "ok", "okay", "yes", "no", "yeah", "yep", "nope", "sure", "thanks",
    "thank you", "haha", "lol", "hm", "hmm", "mhm", "hi", "hey", "hello",
    "good", "nice", "cool", "right", "agreed", "understood",
}

# Compaction threshold — compact oldest turns when session exceeds this
COMPACT_THRESHOLD = 8
COMPACT_KEEP_RAW = 4  # Keep this many recent turns verbatim after compaction


def _wants_recall(message: str) -> bool:
    lower = message.lower()
    return any(kw in lower for kw in RECALL_KEYWORDS)


# ── Lifecycle ────────────────────────────────────────────────────────────────


async def run_migration() -> None:
    """Run all companion SQL migrations on startup."""
    migration_dir = Path(__file__).parent.parent / "migrations"
    migration_files = sorted(migration_dir.glob("*.sql"))

    pool = get_pool()
    for migration_path in migration_files:
        sql = migration_path.read_text()
        try:
            async with pool.connection() as conn:
                await conn.execute(sql)
                await conn.commit()
            logger.info("Migration %s applied", migration_path.name)
        except Exception as e:
            logger.warning("Migration %s may already be applied: %s", migration_path.name, e)


async def generate_daily_recap(affection_level: int) -> str | None:
    """Generate a daily recap by summarizing today's messages via LLM."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        pool = get_pool()
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT role, content FROM companion_messages "
                    "WHERE created_at::date = %s::date ORDER BY created_at ASC LIMIT 40",
                    (today,),
                )
            ).fetchall()

        if len(rows) < 4:
            return None  # Not enough conversation to recap

        conversation = "\n".join(
            f"{'Commander' if r[0] == 'user' else 'Klukai'}: {r[1][:150]}"
            for r in rows[-20:]
        )

        if affection_level >= 3:
            tone = "Write warmly. You care about this Commander deeply."
        elif affection_level >= 1:
            tone = "Write professionally but with subtle investment."
        else:
            tone = "Write coldly and efficiently."

        prompt = (
            f"You are Klukai writing a brief evening operational log about today's "
            f"interactions with the Commander. {tone} Summarize in 2-3 sentences. "
            f"Use first person. Reference specific topics discussed.\n\n{conversation}"
        )

        config = await router.route("recap", SessionState(conversation_id="recap"))
        full = []
        async for token in router.stream(prompt, [{"role": "user", "content": prompt}], config):
            full.append(token)
        return "".join(full) if full else None
    except Exception as e:
        logger.warning("Daily recap generation failed: %s", e)
        return None


async def proactive_callback(message: str) -> None:
    """Deliver a proactive message via WebSocket or push notification."""
    if ws.is_connected("default"):
        await ws.send_proactive("default", message)
    else:
        await send_push(title="Klukai", body=message)


async def _keepalive_loop() -> None:
    """Periodically ping LM Studio models to keep them loaded in VRAM."""
    from .llm_router import _KEEPALIVE_INTERVAL
    while True:
        await asyncio.sleep(_KEEPALIVE_INTERVAL)
        try:
            await router.keepalive()
        except Exception as e:
            logger.warning("Keepalive loop error: %s", e)

_keepalive_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown."""
    global _keepalive_task
    await init_pool(min_size=2, max_size=10)
    await run_migration()
    await memory.init()
    await router.init()
    await mcp.init()
    await affection.init()
    proactive.set_callback(proactive_callback)
    proactive.set_recap_callback(generate_daily_recap)
    proactive.start()
    await events_init()
    load_personality()

    # Warm up primary chat model on startup
    logger.info("Warming up primary LLM model...")
    try:
        await router.keepalive()
        logger.info("LLM warmup complete")
    except Exception as e:
        logger.warning("LLM warmup failed (will retry on first message): %s", e)

    # Start periodic keepalive to prevent model eviction (25-min TTL)
    _keepalive_task = asyncio.create_task(_keepalive_loop())
    logger.info("Klukai companion core started (keepalive every 20 min)")

    yield

    if _keepalive_task:
        _keepalive_task.cancel()
    proactive.stop()
    await events_close()
    await memory.close()
    await router.close()
    await mcp.close()
    await affection.close()
    await close_pool()
    logger.info("Klukai companion core stopped")


app = FastAPI(title="Companion Core", version="0.1.0", lifespan=lifespan)

# Serve Flutter PWA static files (mounted last so API routes take priority)
static_dir = Path("/app/static")
if static_dir.exists():
    app.mount("/app", StaticFiles(directory=str(static_dir), html=True), name="pwa")


# ── Health ───────────────────────────────────────────────────────────────────


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


# ── Push subscription ───────────────────────────────────────────────────────


@app.get("/api/vapid-key")
async def vapid_key():
    return {"key": get_vapid_public_key()}


@app.post("/api/push/subscribe")
async def push_subscribe(sub: dict):
    add_subscription(sub)
    return {"ok": True}


# ── Affection state ─────────────────────────────────────────────────────────


@app.get("/api/affection")
async def get_affection():
    state = await affection.get_state()
    return state.model_dump(mode="json")


# ── TTS proxy (for Flutter UI speaker button) ──────────────────────────


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


# ── Image generation ────────────────────────────────────────────────────


@app.post("/api/generate-image")
async def api_generate_image(req: dict):
    """Generate an image via ComfyUI and save to memory archive."""
    prompt = req.get("prompt", "")
    if not prompt:
        return JSONResponse({"error": "No prompt"}, status_code=400)

    img_bytes = await generate_image(prompt)
    if img_bytes:
        import base64
        # Save to memory archive
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


# ── Gift system ─────────────────────────────────────────────────────────


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

    # Determine gift tier
    if gift_name in prefs.get("loved", []):
        tier, bonus = "loved", 10
    elif gift_name in prefs.get("favoured", []):
        tier, bonus = "favoured", 5
    elif gift_name in prefs.get("liked", []):
        tier, bonus = "liked", 2
    else:
        tier, bonus = "disliked", -1

    # Apply affection bonus directly
    aff_state = await affection.get_state()
    aff_state.score = max(0, min(100, aff_state.score + bonus))
    await affection._save_state(aff_state)

    # Send reaction
    reaction = reactions.get(tier, "...Noted.")
    if ws.is_connected(user_id):
        await ws.send_proactive(user_id, reaction)
        await ws.send_affection(user_id, aff_state.score, aff_state.level, aff_state.level_name, bonus)

    return {"tier": tier, "bonus": bonus, "reaction": reaction, "new_score": aff_state.score}


# ── Mission mode ────────────────────────────────────────────────────────


@app.post("/api/mission")
async def api_mission(req: dict):
    """Send Klukai on a mission. She returns with a report and a gift."""
    user_id = req.get("user", "default")

    if ws.is_connected(user_id):
        await ws.send_proactive(user_id, "Understood, Commander. Deploying for sortie. I will report back shortly.")

    # Generate mission narrative via LLM
    aff_state = await affection.get_state()
    asyncio.create_task(_run_mission(user_id, aff_state.level))
    return {"status": "deployed"}


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

        # Affection bonus for missions
        aff = await affection.get_state()
        aff.score = min(100, aff.score + 3)
        await affection._save_state(aff)
    except Exception as e:
        logger.warning("Mission narrative failed: %s", e)
        if ws.is_connected(user_id):
            await ws.send_proactive(user_id, f"Sortie complete. Found {gift}. Take it, Commander.")


# ── Milestones ──────────────────────────────────────────────────────────


@app.get("/api/milestones")
async def api_milestones():
    """Get all recorded relationship milestones."""
    milestones = await memory.get_milestones()
    return {"milestones": milestones}


# ── Costume ─────────────────────────────────────────────────────────────

_current_costume = "blazing_star"


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


# ── STT proxy ───────────────────────────────────────────────────────────


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


# ── Conversation history ────────────────────────────────────────────────────


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
        # Return in chronological order
        messages.reverse()
        return {"messages": messages}
    except Exception as e:
        logger.error("Failed to fetch messages: %s", e)
        return {"messages": []}


# ── Memory archive API ──────────────────────────────────────────────────────
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


async def _handle_tap_interact(user_id: str) -> None:
    """Handle tap interaction — deliver a short proactive comment."""
    if proactive and proactive._can_send():
        await proactive.trigger_tap()
    else:
        # Fallback: send a simple acknowledgment if proactive can't send
        await ws.send_proactive(user_id, "Hm? Right here, Commander.")


# ── WebSocket ────────────────────────────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    user_id = websocket.query_params.get("user", "default")
    await ws.connect(websocket, user_id)

    # Ensure session exists — always restore mood from PostgreSQL (source of truth)
    session_key = f"{SESSION_ID}:{user_id}" if user_id != "default" else SESSION_ID
    session = await memory.get_session(session_key)

    # Always restore mood from persistent state (PostgreSQL is source of truth)
    restored_mood = "composed"
    try:
        async with get_conn() as conn:
            row = await (await conn.execute(
                "SELECT mood FROM companion_persistent_state WHERE user_id = %s",
                (user_id,),
            )).fetchone()
            if row and row[0]:
                restored_mood = row[0]
    except Exception as e:
        logger.warning("Failed to restore persistent mood: %s", e)

    if session is None:
        conv_id = new_id()
        session = SessionState(conversation_id=conv_id, mood=restored_mood)
        await memory.save_session(session_key, session)
        await _create_conversation(conv_id)
        logger.info("New session created with restored mood '%s'", restored_mood)
    elif session.mood != restored_mood and restored_mood != "composed":
        # Existing session but mood drifted — restore from DB
        session.mood = restored_mood
        await memory.save_session(session_key, session)
        logger.info("Session mood corrected to '%s' from persistent state", restored_mood)

    # Send restored mood to frontend immediately on connect
    if restored_mood != "composed":
        await ws.send_mood(user_id, restored_mood)

    try:
        while True:
            data = await ws.receive(user_id)
            if data is None:
                break

            msg_type = data.get("type")

            if msg_type == "message":
                await _handle_message(data.get("content", ""), session, user_id)
            elif msg_type == "typing":
                pass
            elif msg_type == "voice_end":
                audio = data.get("audio")
                if audio:
                    await _handle_voice(audio, session)
            elif msg_type == "tap_interact":
                await _handle_tap_interact(user_id)
    except WebSocketDisconnect:
        pass
    finally:
        await ws.disconnect(user_id, websocket)


async def _handle_message(content: str, session: SessionState, user_id: str = "default") -> None:
    """Process a text message: memory recall, LLM, response, extraction."""
    if not content.strip():
        return

    start = time.monotonic()
    proactive.mark_responded()

    # Add user turn to session
    session = await memory.add_turn(SESSION_ID, "user", content, session)

    # Skip expensive memory recall for very short messages (hi, ok, yes, etc)
    is_short = len(content.strip()) <= 20
    if is_short:
        episode_memories, rel_facts, recalled_exchanges = [], {}, []
    else:
        episode_memories, rel_facts, recalled_exchanges = await memory.recall_for_prompt(content)

    # Get affection state for prompt modulation
    aff_state = await affection.get_state()

    # Assemble system prompt
    # Calculate days together
    days = 0
    if aff_state.first_interaction:
        try:
            fi = aff_state.first_interaction
            now = datetime.now(fi.tzinfo) if fi.tzinfo else datetime.now()
            days = max(0, (now - fi).days)
        except Exception as e:
            logger.debug("Days-together calculation failed: %s", e)
            days = 0

    system_prompt = assemble_system_prompt(
        mood=session.mood,
        memories=episode_memories,
        relationship_facts=rel_facts,
        recalled_exchanges=recalled_exchanges,
        tools_available=True,
        affection_score=aff_state.score,
        affection_level=aff_state.level,
        days_together=days,
        last_msg_length=len(content),
    )

    # Memory nudge — proactive past reference based on affection level
    nudge = await memory.get_memory_nudge(session.turn_count, aff_state.level)
    if nudge:
        system_prompt += f"\n\n{nudge}"

    # Build messages for LLM — use summary + recent turns for compact context
    messages = []
    if session.context_summary:
        messages.append({
            "role": "system",
            "content": f"[Earlier conversation summary: {session.context_summary}]",
        })
    messages.extend(
        {"role": t["role"], "content": t["content"]}
        for t in session.turns[-16:]
    )

    # Check if this needs the agentic tool-use loop
    use_agent = await router.needs_agent(content)
    msg_id = new_id()

    if use_agent:
        # Agentic path: think, use tools, then respond (agent loop sends its own thinking events)
        logger.info("Routing to agent loop (tools needed)")
        agent = AgentLoop(router, mcp, ws)
        agent_result = await agent.run(system_prompt, messages)

        response_text = _fix_narration(agent_result.response)
        model_name = agent_result.model

        # Stream the final response token by token for the UI
        for token in _chunk_text(response_text, 8):
            await ws.send_token(user_id, token)
            await asyncio.sleep(0.02)

        latency_ms = int((time.monotonic() - start) * 1000)
        await ws.send_done(user_id, msg_id, model_name)

        logger.info(
            "Agent loop: %d iterations, %d tools used (%s)",
            agent_result.iterations,
            len(agent_result.tools_used),
            ", ".join(agent_result.tools_used) or "none",
        )
    else:
        # Direct path: stream from LLM
        await ws.send_thinking(user_id, "Composing response...")
        config = await router.route(content, session)
        logger.info("Routing to %s/%s", config.provider, config.model)

        # Fire a delayed "warming up" message if model is slow to respond
        warmup_timer: asyncio.Task | None = None
        if config.provider == "lmstudio":
            async def _warmup_notify():
                await asyncio.sleep(8.0)
                await ws.send_thinking(user_id, "Loading neural pathways, Commander. Stand by...")
            warmup_timer = asyncio.create_task(_warmup_notify())

        full_response = []
        buffer = ""
        first_flush = True
        async for token in router.stream(system_prompt, messages, config):
            # Cancel warmup message once first token arrives
            if warmup_timer and not warmup_timer.done():
                warmup_timer.cancel()
                warmup_timer = None
            full_response.append(token)
            buffer += token
            # First flush after 20 chars for fast perceived response, then sentence boundaries
            flush_threshold = 20 if first_flush else 80
            if any(c in buffer for c in '.!?\n)') or len(buffer) > flush_threshold:
                await ws.send_token(user_id, buffer)
                buffer = ""
                first_flush = False
        if warmup_timer and not warmup_timer.done():
            warmup_timer.cancel()
        if buffer:
            await ws.send_token(user_id, buffer)

        # Apply narration fix only to the complete text (avoids split-pattern bugs)
        raw_text = "".join(full_response)
        response_text = _fix_narration(raw_text)
        model_name = config.model
        latency_ms = int((time.monotonic() - start) * 1000)

        # Send corrected final text if narration fix changed anything
        final_text = response_text if response_text != raw_text else None
        await ws.send_done(user_id, msg_id, model_name, final_text=final_text)

    # Add assistant turn to session
    session = await memory.add_turn(SESSION_ID, "assistant", response_text, session)

    # Store messages in PostgreSQL + mark as read
    await _store_message(session.conversation_id, "user", content, model_name)
    try:
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "UPDATE companion_messages SET read_at = NOW() "
                "WHERE conversation_id = %s AND role = 'user' AND read_at IS NULL",
                (session.conversation_id,),
            )
    except Exception as e:
        logger.warning("Failed to set read_at: %s", e)
    await ws.send(user_id, {"type": "read_receipt", "read_at": datetime.now().isoformat()})

    await _store_message(
        session.conversation_id, "assistant", response_text, model_name,
        latency_ms=latency_ms,
    )

    # Commander save/discard overrides — operate on the most recent memory
    content_lower = content.lower()
    if any(kw in content_lower for kw in SAVE_KEYWORDS) and _last_memory_id:
        asyncio.create_task(_do_memory_keep(_last_memory_id, kept=True))
    elif any(kw in content_lower for kw in DISCARD_KEYWORDS) and _last_memory_id:
        asyncio.create_task(_do_memory_keep(_last_memory_id, kept=False))

    # Track whether image gen was triggered (for extraction curation pass)
    image_triggered = False
    triggered_memory_id: str | None = None

    # Background tasks — only if main LLM succeeded (not a fallback error)
    if not response_text.startswith("Communications disrupted"):
        # Recall detection takes priority over new image generation
        if _wants_recall(content):
            logger.info("Memory recall triggered for: %s", content[:80])
            asyncio.create_task(_background_recall(content, session, user_id))
        elif needs_image(content):
            logger.info("Image generation triggered for: %s", content[:80])
            # Build chat context from last few turns for scene-aware prompting
            recent_turns = session.turns[-6:]  # Last 3 exchanges
            chat_ctx = "\n".join(
                f"{t['role']}: {t['content'][:200]}" for t in recent_turns
            )
            image_triggered = True
            # _background_image_gen sets _last_memory_id; we'll read it in extraction
            asyncio.create_task(_background_image_gen(content, chat_context=chat_ctx))

    # Background: extract facts and create episodes
    # Skip extraction for trivial messages (no facts to extract, saves a full LLM round-trip)
    content_stripped = content.strip().lower().rstrip("!.?)")
    is_trivial = content_stripped in TRIVIAL_PATTERNS or (
        len(content_stripped) <= 5 and not image_triggered
    )

    if not is_trivial:
        asyncio.create_task(_background_extraction(
            content, response_text, session, user_id,
            image_generated=image_triggered,
        ))
    else:
        logger.info("Trivial message, skipping extraction: %s", content[:40])

    # Background: compact session if turns exceed threshold
    if len(session.turns) >= COMPACT_THRESHOLD:
        asyncio.create_task(_background_compaction(session))


async def _handle_voice(audio_b64: str, session: SessionState) -> None:
    """Process voice: STT -> text -> LLM -> TTS -> audio."""
    voice_url = os.environ.get("VOICE_URL", "http://companion-voice:8301")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # STT
            await ws.send_thinking("default", "Listening...")
            r = await client.post(
                f"{voice_url}/stt",
                json={"audio": audio_b64},
            )
            r.raise_for_status()
            transcript = r.json().get("text", "")

            if not transcript.strip():
                return

            # Process as text message (which streams the text response)
            await _handle_message(transcript, session)

            # Get the last assistant response for TTS
            session = await memory.get_session(SESSION_ID)
            if session and session.turns:
                last_turn = session.turns[-1]
                if last_turn["role"] == "assistant":
                    # TTS
                    r = await client.post(
                        f"{voice_url}/tts",
                        json={"text": last_turn["content"]},
                    )
                    if r.status_code == 200:
                        import base64
                        audio_out = base64.b64encode(r.content).decode()
                        await ws.send_voice("default", audio_out, final=True)
    except Exception as e:
        logger.error("Voice processing failed: %s", e)


async def _background_extraction(
    user_msg: str,
    assistant_msg: str,
    session: SessionState,
    user_id: str = "default",
    image_generated: bool = False,
    memory_id: str | None = None,
) -> None:
    """Background task: extract facts, adjust affection, and maybe create an episode.

    Delayed 3s so LLM calls don't compete with the main response stream.
    All calls are serialized (not parallel) to avoid queuing in LM Studio.
    """
    await asyncio.sleep(3)  # Let main response finish streaming first
    try:
        aff_state_bg = await affection.get_state()
        result = await extract_facts(
            user_msg, assistant_msg,
            image_generated=image_generated,
            affection_level=aff_state_bg.level,
        )

        # Apply curation if image was generated and curation data came back
        # Resolve memory_id: prefer explicit arg, fall back to module-level _last_memory_id
        # (image gen runs concurrently and sets it ~1-30s before extraction completes)
        curation_target = memory_id or (image_generated and _last_memory_id) or None
        if image_generated and curation_target and "memory_curation" in result:
            try:
                await memory_archive.update_curation(
                    curation_target, result["memory_curation"], aff_state_bg.level
                )
            except Exception as e:
                logger.warning("Memory curation update failed for %s: %s", curation_target, e)

        # Store new facts
        for fact in result.get("facts", []):
            await memory.set_relationship_fact(fact["key"], fact["value"])

        # Update mood in session + persist to PostgreSQL
        mood = result.get("mood", "composed")
        session.mood = mood
        await memory.save_session(SESSION_ID, session)
        await ws.send_mood("default", mood)
        proactive.set_last_mood(mood)

        # Persist mood so it survives session expiry
        try:
            async with get_conn_autocommit() as conn:
                await conn.execute(
                    "INSERT INTO companion_persistent_state (user_id, mood, last_conversation_id, updated_at) "
                    "VALUES (%s, %s, %s, NOW()) "
                    "ON CONFLICT (user_id) DO UPDATE SET mood = %s, last_conversation_id = %s, updated_at = NOW()",
                    (user_id, mood, session.conversation_id, mood, session.conversation_id),
                )
        except Exception as e:
            logger.warning("Failed to persist mood: %s", e)

        # Adjust affection based on interaction
        try:
            aff_change = await affection.classify_and_adjust(user_msg, assistant_msg)

            # Sync affection level to proactive engine
            proactive.set_affection_level(aff_change.new_level)

            # Send real-time affection update to UI
            await ws.send_affection("default", 
                aff_change.new_score, aff_change.new_level,
                aff_change.new_level_name, aff_change.delta,
            )

            # Handle level transitions
            if aff_change.level_changed:
                await ws.send_affection_level_change("default", 
                    aff_change.new_level, aff_change.new_level_name,
                    aff_change.level_direction,
                )

                personality = load_personality()
                aff_config = personality.get("affection", {})

                if aff_change.level_direction == "up":
                    # Check for first-time milestone scene
                    milestone_key = f"affection_level_{aff_change.new_level}"
                    is_new = await memory.record_milestone(milestone_key)

                    if is_new:
                        # First time reaching this level — deliver milestone scene
                        scenes = aff_config.get("milestone_scenes", {})
                        scene_lines = scenes.get(aff_change.new_level, [])
                        for line in scene_lines:
                            await ws.send_proactive("default", line)
                            await asyncio.sleep(2)  # Pause between lines
                    else:
                        # Repeat level-up — just the short message
                        messages = aff_config.get("level_up_messages", {})
                        special_line = messages.get(aff_change.new_level)
                        if special_line:
                            await ws.send_proactive("default", special_line)
                else:
                    messages = aff_config.get("level_down_messages", {})
                    special_line = messages.get(aff_change.new_level)
                    if special_line:
                        await ws.send_proactive("default", special_line)

                logger.info(
                    "Affection level %s: %d -> %d (%s)",
                    aff_change.level_direction,
                    aff_change.new_level - (1 if aff_change.level_direction == "up" else -1),
                    aff_change.new_level,
                    aff_change.new_level_name,
                )
        except Exception as e:
            logger.warning("Affection adjustment failed: %s", e)

        # Store exchange in conversation memory for rich recall
        try:
            exchange_id = str(uuid.uuid4())
            await memory.store_exchange(
                exchange_id=exchange_id,
                user_content=user_msg,
                assistant_content=assistant_msg,
                topics=result.get("topics", []),
                mood=result.get("mood", "composed"),
                importance=0.7 if result.get("should_remember") else 0.4,
                conversation_id=session.conversation_id,
            )
        except Exception as e:
            logger.warning("Exchange storage failed: %s", e)

        # Create episode every 10 turns
        if session.turn_count > 0 and session.turn_count % 10 == 0:
            summary = await create_episode_summary(session.turns)
            if summary:
                episode_id = str(uuid.uuid4())
                await memory.store_episode(
                    episode_id=episode_id,
                    summary=summary,
                    keywords=result.get("topics", []),
                    emotion_tags=[mood],
                    importance=0.5,
                    conversation_id=session.conversation_id,
                )
                logger.info("Episode stored: %s", summary[:80])
    except Exception as e:
        logger.error("Background extraction failed: %s", e)


async def _background_compaction(session: SessionState) -> None:
    """Compact older session turns into a summary to reduce prefill tokens.

    Triggered when session.turns >= COMPACT_THRESHOLD. Summarizes the oldest
    turns via gemma-4-e2b-it, keeps the last COMPACT_KEEP_RAW turns verbatim.
    """
    from .fact_extractor import compact_turns

    try:
        turns = session.turns
        if len(turns) < COMPACT_THRESHOLD:
            return

        # Split: compact the old turns, keep the recent ones raw
        old_turns = turns[:-COMPACT_KEEP_RAW]
        recent_turns = turns[-COMPACT_KEEP_RAW:]

        # Build text to compact (include existing summary if any)
        if session.context_summary:
            old_turns = [{"role": "system", "content": f"[Previous summary: {session.context_summary}]"}] + old_turns

        summary = await compact_turns(old_turns)
        if not summary:
            logger.warning("Compaction returned empty summary, keeping raw turns")
            return

        # Replace session turns with summary + recent raw turns
        session.context_summary = summary
        session.turns = recent_turns
        await memory.save_session(SESSION_ID, session)

        logger.info(
            "Session compacted: %d turns → summary (%d chars) + %d raw turns",
            len(old_turns), len(summary), len(recent_turns),
        )
    except Exception as e:
        logger.error("Background compaction failed: %s", e)


async def _background_image_gen(user_request: str, chat_context: str = "") -> None:
    """Generate an anime image based on the user's request and recent chat context."""
    from .image_gen import check_comfyui_ready

    # Wait for chat response to finish and VRAM to settle
    logger.info("Image gen: waiting 1s for VRAM...")
    await asyncio.sleep(1)
    try:
        logger.info("Image gen: starting for '%s'", user_request[:60])

        # Check if ComfyUI is ready
        ready = await check_comfyui_ready()
        if not ready:
            await ws.send_thinking("default", "Warming up image systems, Commander. Stand by...")
        else:
            await ws.send_thinking("default", "Compiling tactical visualization, Commander...")

        # Use both the request AND recent chat for context-aware generation
        full_context = f"{chat_context}\n{user_request}" if chat_context else user_request
        couple = is_couple_scene(full_context)

        # Get affection level for mood-aware prompts
        aff_state = await affection.get_state()
        aff_level = aff_state.level

        scene_tags = _enhance_image_prompt(full_context, couple=couple)
        full_prompt = build_prompt(
            scene_tags, couple=couple, affection_level=aff_level, context=full_context,
        )
        logger.info("Image prompt (aff=%d): %s", aff_level, full_prompt[:300])

        # Determine orientation
        if is_landscape(full_context):
            width, height = 1216, 832
        else:
            width, height = 832, 1216

        img_bytes = await generate_image(full_prompt, width=width, height=height)
        if img_bytes:
            import base64 as b64
            global _last_memory_id

            # Get current session state for archive metadata
            session_for_save = await memory.get_session(SESSION_ID)
            conv_id = session_for_save.conversation_id if session_for_save else "unknown"
            session_mood = session_for_save.mood if session_for_save else "composed"

            # Save to memory archive before sending to UI
            memory_id = await memory_archive.save_image(
                img_bytes, full_prompt, conv_id, session_mood, aff_level,
            )
            if memory_id:
                _last_memory_id = memory_id
                logger.info("Image archived as memory %s", memory_id)

            img_b64 = b64.b64encode(img_bytes).decode()
            await ws.send("default", {"type": "image", "data": img_b64, "memory_id": memory_id})
            logger.info("Image sent to UI (%d bytes)", len(img_bytes))
        else:
            await ws.send_proactive("default", "...Visualization failed. Interference in the rendering pipeline. I'll try again later.")
    except Exception as e:
        logger.error("Background image gen failed: %s", e)


async def _background_recall(content: str, session: SessionState, user_id: str) -> None:
    """Retrieve a memory from the archive and send it to the UI as a proactive message + image."""
    try:
        aff_state = await affection.get_state()
        mem = await memory_archive.recall_memory(content, session.mood, aff_state.level)
        if not mem:
            await ws.send_proactive(user_id, "...I searched through our records, but couldn't find anything matching that.")
            return

        annotation = mem.get("annotation") or "A moment I've preserved."
        await ws.send_proactive(user_id, annotation)

        img_bytes = await memory_archive.get_image_bytes(mem["id"], thumbnail=False)
        if img_bytes:
            import base64 as b64
            img_b64 = b64.b64encode(img_bytes).decode()
            await ws.send(user_id, {"type": "image", "data": img_b64, "memory_id": mem["id"]})
            logger.info("Recalled memory %s sent to UI", mem["id"])
    except Exception as e:
        logger.error("Background recall failed: %s", e)


async def _do_memory_keep(memory_id: str, kept: bool) -> None:
    """Apply a commander save/discard override to a memory."""
    try:
        kept_by = "commander" if kept else "discarded"
        ok = await memory_archive.update_kept(memory_id, kept=kept, kept_by=kept_by)
        if ok:
            logger.info("Memory %s: kept=%s by commander", memory_id, kept)
    except Exception as e:
        logger.error("Memory keep/discard failed for %s: %s", memory_id, e)


# ── Helpers ──────────────────────────────────────────────────────────────


def _chunk_text(text: str, chunk_size: int = 8) -> list[str]:
    """Split text into chunks for simulated streaming."""
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def _fix_narration(text: str) -> str:
    """Fix second-person narration and clean up model artifacts."""
    import re
    # Strip R1 reasoning blocks: <think>...</think>
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'<\|think\|>.*?<\|/think\|>', '', text, flags=re.DOTALL)
    # Convert "(You verb...)" to "(I verb...)"
    text = re.sub(r'\(You ([a-z])', lambda m: f'(I {m.group(1)}', text)
    # Convert "(Your noun)" to "(My noun)"
    text = re.sub(r'\(Your ', '(My ', text)
    text = re.sub(r'\(your ', '(my ', text)
    # Strip parentheticals that narrate Commander's actions/appearance
    text = re.sub(r'\([^)]*(?:your face|your eyes|your expression|your mouth|crosses your|touches your)[^)]*\)', '', text)
    # Strip trailing pipe characters (dolphin-glm reasoning artifact)
    # Only strip pipes, not newlines (newlines are paragraph formatting)
    while text.endswith('|'):
        text = text[:-1]
    text = text.rstrip(' ')
    # Clean up double spaces from removals
    text = re.sub(r'  +', ' ', text)
    return text


def _enhance_image_prompt(user_request: str, couple: bool = False) -> str:
    """Fast keyword-based tag generation — no LLM call needed."""
    lower = user_request.lower()
    tags = []

    # Scene/setting tags
    SCENE_MAP = {
        "sunset": "sunset, orange sky, golden hour lighting",
        "night": "night, moonlight, dark sky, stars",
        "rain": "rain, wet, umbrella, overcast",
        "snow": "snow, winter, cold breath, scarf",
        "beach": "beach, ocean, sand, swimsuit, summer",
        "cafe": "cafe, table, coffee cup, indoor, cozy",
        "battle": "battlefield, smoke, debris, action pose",
        "motorcycle": "motorcycle, riding, wind, speed lines, road",
        "bed": "bedroom, bed, pillows, soft lighting, intimate",
        "rooftop": "rooftop, city skyline, wind, evening",
        "garden": "garden, flowers, natural lighting, peaceful",
        "office": "office, desk, computer, indoor lighting",
        "forest": "forest, trees, nature, sunlight through leaves",
        "city": "city, urban, street, buildings, neon",
    }
    for keyword, scene_tags in SCENE_MAP.items():
        if keyword in lower:
            tags.append(scene_tags)

    # Mood/action tags
    MOOD_MAP = {
        "kiss": "kiss, eyes closed, romantic",
        "hug": "hug, embrace, close, warm",
        "cuddle": "cuddling, lying down, comfortable, close",
        "hold": "holding hands, close, side by side",
        "smile": "smile, happy, cheerful",
        "blush": "blush, embarrassed, looking away",
        "cry": "tears, emotional, sad",
        "fight": "fighting stance, action, dynamic pose",
        "sleep": "sleeping, peaceful, eyes closed",
        "eat": "eating, food, table",
        "cook": "cooking, kitchen, apron",
        "read": "reading, book, sitting, quiet",
    }
    for keyword, mood_tags in MOOD_MAP.items():
        if keyword in lower:
            tags.append(mood_tags)

    # Situational context from conversation
    for keyword, sit_tags in SITUATION_KEYWORDS.items():
        if keyword in lower:
            tags.append(sit_tags)

    # Squad member detection — add their character tags
    for member, member_tags in SQUAD_KEYWORDS.items():
        if member in lower:
            tags.append(member_tags)
            tags.append("multiple girls" if not couple else "")

    # If no specific tags matched, add generic scene
    if not tags:
        tags.append("standing, looking at viewer, detailed background")

    return ", ".join(t for t in tags if t)


def _strip_actions_for_tts(text: str) -> str:
    """Remove all parenthetical actions from text for natural voice output."""
    import re
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ── DB helpers ───────────────────────────────────────────────────────────────


async def _create_conversation(conv_id: str) -> None:
    try:
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "INSERT INTO companion_conversations (id) VALUES (%s) "
                "ON CONFLICT DO NOTHING",
                (conv_id,),
            )
    except Exception as e:
        logger.error("Failed to create conversation: %s", e)


async def _store_message(
    conversation_id: str,
    role: str,
    content: str,
    model: str = "",
    latency_ms: int | None = None,
) -> None:
    try:
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "INSERT INTO companion_messages "
                "(conversation_id, role, content, model, latency_ms) "
                "VALUES (%s, %s, %s, %s, %s)",
                (conversation_id, role, content, model, latency_ms),
            )
            await conn.execute(
                "UPDATE companion_conversations SET turn_count = turn_count + 1, "
                "model_used = %s WHERE id = %s",
                (model, conversation_id),
            )
    except Exception as e:
        logger.error("Failed to store message: %s", e)


# ── Root redirect ────────────────────────────────────────────────────────────


@app.get("/")
async def root():
    """Redirect to PWA or show status."""
    if static_dir.exists() and (static_dir / "index.html").exists():
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/app/")
    return {"status": "companion-core running", "pwa": "not built yet"}
