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
import psycopg
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .affection import AffectionManager
from .agent_loop import AgentLoop
from .image_gen import generate_image, needs_image, build_prompt, is_couple_scene
from .fact_extractor import create_episode_summary, extract_facts
from .llm_router import LLMRouter
from .mcp_client import MCPClient
from .memory import MemoryManager
from .models import SessionState, new_id
from .personality import assemble_system_prompt, load_personality
from .proactive import ProactiveEngine
from .push import add_subscription, get_vapid_public_key, send_push
from .ws_manager import WSManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://aichat:aichat@aichat-db:5432/aichat"
)

# ── Globals ──────────────────────────────────────────────────────────────────

memory = MemoryManager()
router = LLMRouter()
mcp = MCPClient()
ws = WSManager()
proactive = ProactiveEngine()
affection = AffectionManager()

SESSION_ID = "default"  # Single-user, single session


# ── Lifecycle ────────────────────────────────────────────────────────────────


async def run_migration() -> None:
    """Run all companion SQL migrations on startup."""
    migration_dir = Path(__file__).parent.parent / "migrations"
    migration_files = sorted(migration_dir.glob("*.sql"))

    for migration_path in migration_files:
        sql = migration_path.read_text()
        try:
            async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
                await conn.execute(sql)
                await conn.commit()
            logger.info("Migration %s applied", migration_path.name)
        except Exception as e:
            logger.warning("Migration %s may already be applied: %s", migration_path.name, e)


async def generate_daily_recap(affection_level: int) -> str | None:
    """Generate a daily recap by summarizing today's messages via LLM."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
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

        config = router.route("recap", SessionState(conversation_id="recap"))
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown."""
    await run_migration()
    await memory.init()
    await router.init()
    await mcp.init()
    await affection.init()
    proactive.set_callback(proactive_callback)
    proactive.set_recap_callback(generate_daily_recap)
    proactive.start()
    load_personality()
    logger.info("Klukai companion core started")
    yield
    proactive.stop()
    await memory.close()
    await router.close()
    await mcp.close()
    await affection.close()
    logger.info("Klukai companion core stopped")


app = FastAPI(title="Companion Core", version="0.1.0", lifespan=lifespan)

# Serve Flutter PWA static files (mounted last so API routes take priority)
static_dir = Path("/app/static")
if static_dir.exists():
    app.mount("/app", StaticFiles(directory=str(static_dir), html=True), name="pwa")


# ── Health ───────────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok", "service": "companion-core", "version": "0.1.0"}


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
    """Generate an image via ComfyUI."""
    prompt = req.get("prompt", "")
    if not prompt:
        return JSONResponse({"error": "No prompt"}, status_code=400)

    img_bytes = await generate_image(prompt)
    if img_bytes:
        import base64
        return {
            "image": base64.b64encode(img_bytes).decode(),
            "format": "png",
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
    old_score = aff_state.score
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
        config = router.route("mission", SessionState(conversation_id="mission"))
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
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
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


# ── WebSocket ────────────────────────────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    user_id = websocket.query_params.get("user", "default")
    await ws.connect(websocket, user_id)

    # Ensure session exists
    session_key = f"{SESSION_ID}:{user_id}" if user_id != "default" else SESSION_ID
    session = await memory.get_session(session_key)
    if session is None:
        conv_id = new_id()
        session = SessionState(conversation_id=conv_id)
        await memory.save_session(session_key, session)
        await _create_conversation(conv_id)

    try:
        while True:
            data = await ws.receive(user_id)
            if data is None:
                break

            msg_type = data.get("type")

            if msg_type == "message":
                await _handle_message(data.get("content", ""), session)
            elif msg_type == "typing":
                pass
            elif msg_type == "voice_end":
                audio = data.get("audio")
                if audio:
                    await _handle_voice(audio, session)
    except WebSocketDisconnect:
        pass
    finally:
        await ws.disconnect(user_id)


async def _handle_message(content: str, session: SessionState) -> None:
    """Process a text message: memory recall, LLM, response, extraction."""
    if not content.strip():
        return

    start = time.monotonic()
    proactive.mark_responded()

    # Add user turn to session
    session = await memory.add_turn(SESSION_ID, "user", content, session)

    # Recall relevant memories + past conversation exchanges
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
        except Exception:
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

    # Build messages for LLM
    messages = [
        {"role": t["role"], "content": t["content"]}
        for t in session.turns[-20:]
    ]

    # Check if this needs the agentic tool-use loop
    use_agent = router.needs_agent(content)
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
            await ws.send_token("default", token)
            await asyncio.sleep(0.02)

        latency_ms = int((time.monotonic() - start) * 1000)
        await ws.send_done("default", msg_id, model_name)

        logger.info(
            "Agent loop: %d iterations, %d tools used (%s)",
            agent_result.iterations,
            len(agent_result.tools_used),
            ", ".join(agent_result.tools_used) or "none",
        )
    else:
        # Direct path: stream from LLM
        await ws.send_thinking("default", "Composing response...")
        config = router.route(content, session)
        logger.info("Routing to %s/%s", config.provider, config.model)

        full_response = []
        buffer = ""
        first_flush = True
        async for token in router.stream(system_prompt, messages, config):
            full_response.append(token)
            buffer += token
            # First flush after 20 chars for fast perceived response, then sentence boundaries
            flush_threshold = 20 if first_flush else 80
            if any(c in buffer for c in '.!?\n)') or len(buffer) > flush_threshold:
                fixed = _fix_narration(buffer)
                await ws.send_token("default", fixed)
                buffer = ""
                first_flush = False
        if buffer:
            await ws.send_token("default", _fix_narration(buffer))

        response_text = _fix_narration("".join(full_response))
        model_name = config.model
        latency_ms = int((time.monotonic() - start) * 1000)

        await ws.send_done("default", msg_id, model_name)

    # Add assistant turn to session
    session = await memory.add_turn(SESSION_ID, "assistant", response_text, session)

    # Store messages in PostgreSQL
    await _store_message(session.conversation_id, "user", content, model_name)
    await _store_message(
        session.conversation_id, "assistant", response_text, model_name,
        latency_ms=latency_ms,
    )

    # Background: extract facts and create episodes
    asyncio.create_task(_background_extraction(content, response_text, session))

    # Background: generate TTS audio for the response
    asyncio.create_task(_background_tts(response_text))

    # Background: generate image if the message requested one
    if needs_image(content):
        logger.info("Image generation triggered for: %s", content[:80])
        asyncio.create_task(_background_image_gen(content))


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
    user_msg: str, assistant_msg: str, session: SessionState
) -> None:
    """Background task: extract facts, adjust affection, and maybe create an episode."""
    try:
        result = await extract_facts(user_msg, assistant_msg)

        # Store new facts
        for fact in result.get("facts", []):
            await memory.set_relationship_fact(fact["key"], fact["value"])

        # Update mood in session
        mood = result.get("mood", "composed")
        session.mood = mood
        await memory.save_session(SESSION_ID, session)
        await ws.send_mood("default", mood)
        proactive.set_last_mood(mood)

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


# ── Background TTS ───────────────────────────────────────────────────────


async def _background_image_gen(user_request: str) -> None:
    """Generate an anime image based on the user's request and send via WebSocket."""
    # Wait for chat response to finish and VRAM to settle
    logger.info("Image gen: waiting 5s for VRAM...")
    await asyncio.sleep(5)
    try:
        logger.info("Image gen: starting for '%s'", user_request[:60])
        await ws.send_proactive("default", "Compiling tactical visualization, Commander. Stand by.")

        # Detect if this is a couple scene
        couple = is_couple_scene(user_request)

        # Enhance the prompt with LLM
        scene_tags = await _enhance_image_prompt(user_request, couple=couple)
        full_prompt = build_prompt(scene_tags, couple=couple)
        logger.info("Image prompt: %s", full_prompt[:200])

        # Determine orientation from request
        landscape_keywords = ["sunset", "landscape", "horizon", "riding", "motorcycle", "driving", "panorama"]
        if any(kw in user_request.lower() for kw in landscape_keywords):
            width, height = 1216, 832
        else:
            width, height = 832, 1216

        # Generate
        img_bytes = await generate_image(full_prompt, width=width, height=height)
        if img_bytes:
            import base64 as b64
            img_b64 = b64.b64encode(img_bytes).decode()
            await ws.send("default", {"type": "image", "data": img_b64})
            logger.info("Image sent to UI (%d bytes)", len(img_bytes))
        else:
            await ws.send_proactive("default", "...Visualization failed. Interference in the rendering pipeline. I'll try again later.")
    except Exception as e:
        logger.error("Background image gen failed: %s", e)


async def _background_tts(text: str) -> None:
    """Generate TTS audio and send via WebSocket."""
    voice_url = os.environ.get("VOICE_URL", "http://companion-voice:8301")
    tts_text = _strip_actions_for_tts(text)
    if not tts_text.strip() or len(tts_text) > 500:
        return

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{voice_url}/tts",
                json={"text": tts_text[:500], "language": "en"},
            )
            if r.status_code == 200:
                import base64 as b64
                audio_b64 = b64.b64encode(r.content).decode()
                await ws.send_voice("default", audio_b64, final=True)
                logger.info("TTS audio sent (%d bytes)", len(r.content))
            else:
                logger.warning("TTS returned %d: %s", r.status_code, r.text[:100])
    except Exception as e:
        logger.debug("TTS unavailable: %s", e)


# ── Helpers ──────────────────────────────────────────────────────────────


def _chunk_text(text: str, chunk_size: int = 8) -> list[str]:
    """Split text into chunks for simulated streaming."""
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def _fix_narration(text: str) -> str:
    """Fix second-person narration: convert (You ...) to (I ...) and strip Commander narration."""
    import re
    # Convert "(You verb...)" to "(I verb...)"
    text = re.sub(r'\(You ([a-z])', lambda m: f'(I {m.group(1)}', text)
    # Convert "(Your noun)" to "(My noun)"
    text = re.sub(r'\(Your ', '(My ', text)
    text = re.sub(r'\(your ', '(my ', text)
    # Strip parentheticals that narrate Commander's actions/appearance
    text = re.sub(r'\([^)]*(?:your face|your eyes|your expression|your mouth|crosses your|touches your)[^)]*\)', '', text)
    # Clean up double spaces from removals
    text = re.sub(r'  +', ' ', text)
    return text


async def _enhance_image_prompt(user_request: str, couple: bool = False) -> str:
    """Use LLM to convert a natural language scene request into Danbooru-style tags."""
    char_desc = (
        "The female character is Klukai: silver hair, green eyes, long ponytail, athletic, military uniform. "
    )
    if couple:
        char_desc += (
            "The male character is the Commander: short dark hair, brown eyes, tan skin, "
            "strong build, military uniform. They are a couple. "
            "IMPORTANT: Include BOTH 1boy and 1girl tags. The male has dark hair, the female has silver hair. "
        )

    prompt = (
        "Convert this scene description into Danbooru-style tags for anime image generation. "
        "Include: characters, setting, mood, lighting, pose, clothing details. "
        f"{char_desc}"
        "Return ONLY comma-separated tags, nothing else.\n\n"
        f"Scene: {user_request}"
    )
    try:
        config = router.route("tags", SessionState(conversation_id="image"))
        tags = []
        async for token in router.stream(prompt, [{"role": "user", "content": prompt}], config):
            tags.append(token)
        return "".join(tags).strip()
    except Exception as e:
        logger.warning("Image prompt enhancement failed: %s", e)
        return user_request


def _strip_actions_for_tts(text: str) -> str:
    """Remove all parenthetical actions from text for natural voice output."""
    import re
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ── DB helpers ───────────────────────────────────────────────────────────────


async def _create_conversation(conv_id: str) -> None:
    try:
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            await conn.execute(
                "INSERT INTO companion_conversations (id) VALUES (%s) "
                "ON CONFLICT DO NOTHING",
                (conv_id,),
            )
            await conn.commit()
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
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
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
            await conn.commit()
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
