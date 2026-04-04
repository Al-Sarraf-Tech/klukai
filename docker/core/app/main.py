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
from .image_gen import generate_image
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
    if ws.connected:
        await ws.send_proactive(message)
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
    await ws.connect(websocket)

    # Ensure session exists
    session = await memory.get_session(SESSION_ID)
    if session is None:
        conv_id = new_id()
        session = SessionState(conversation_id=conv_id)
        await memory.save_session(SESSION_ID, session)
        # Create conversation record
        await _create_conversation(conv_id)

    try:
        while True:
            data = await ws.receive()
            if data is None:
                break

            msg_type = data.get("type")

            if msg_type == "message":
                await _handle_message(data.get("content", ""), session)
            elif msg_type == "typing":
                pass  # Could track typing indicators
            elif msg_type == "voice_end":
                # Voice data would be transcribed first via companion-voice
                audio = data.get("audio")
                if audio:
                    await _handle_voice(audio, session)
    except WebSocketDisconnect:
        pass
    finally:
        await ws.disconnect()


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
    system_prompt = assemble_system_prompt(
        mood=session.mood,
        memories=episode_memories,
        relationship_facts=rel_facts,
        recalled_exchanges=recalled_exchanges,
        tools_available=True,
        affection_score=aff_state.score,
        affection_level=aff_state.level,
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
            await ws.send_token(token)
            await asyncio.sleep(0.02)

        latency_ms = int((time.monotonic() - start) * 1000)
        await ws.send_done(msg_id, model_name)

        logger.info(
            "Agent loop: %d iterations, %d tools used (%s)",
            agent_result.iterations,
            len(agent_result.tools_used),
            ", ".join(agent_result.tools_used) or "none",
        )
    else:
        # Direct path: stream from LLM
        await ws.send_thinking("Composing response...")
        config = router.route(content, session)
        logger.info("Routing to %s/%s", config.provider, config.model)

        full_response = []
        buffer = ""
        async for token in router.stream(system_prompt, messages, config):
            full_response.append(token)
            buffer += token
            # Flush on sentence boundaries or when buffer is large enough to catch patterns
            if any(c in buffer for c in '.!?\n)') or len(buffer) > 80:
                fixed = _fix_narration(buffer)
                await ws.send_token(fixed)
                buffer = ""
        if buffer:
            await ws.send_token(_fix_narration(buffer))

        response_text = _fix_narration("".join(full_response))
        model_name = config.model
        latency_ms = int((time.monotonic() - start) * 1000)

        await ws.send_done(msg_id, model_name)

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


async def _handle_voice(audio_b64: str, session: SessionState) -> None:
    """Process voice: STT -> text -> LLM -> TTS -> audio."""
    voice_url = os.environ.get("VOICE_URL", "http://companion-voice:8301")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # STT
            await ws.send_thinking("Listening...")
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
                        await ws.send_voice(audio_out, final=True)
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
        await ws.send_mood(mood)

        # Adjust affection based on interaction
        try:
            aff_change = await affection.classify_and_adjust(user_msg, assistant_msg)

            # Sync affection level to proactive engine
            proactive.set_affection_level(aff_change.new_level)

            # Send real-time affection update to UI
            await ws.send_affection(
                aff_change.new_score, aff_change.new_level,
                aff_change.new_level_name, aff_change.delta,
            )

            # Handle level transitions
            if aff_change.level_changed:
                await ws.send_affection_level_change(
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
                            await ws.send_proactive(line)
                            await asyncio.sleep(2)  # Pause between lines
                    else:
                        # Repeat level-up — just the short message
                        messages = aff_config.get("level_up_messages", {})
                        special_line = messages.get(aff_change.new_level)
                        if special_line:
                            await ws.send_proactive(special_line)
                else:
                    messages = aff_config.get("level_down_messages", {})
                    special_line = messages.get(aff_change.new_level)
                    if special_line:
                        await ws.send_proactive(special_line)

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
                await ws.send_voice(audio_b64, final=True)
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
