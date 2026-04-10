"""Companion Core: FastAPI application with WebSocket, memory, and LLM routing."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from .agent_loop import AgentLoop
from .background import (
    background_compaction,
    background_extraction,
    background_image_gen,
    background_recall,
    do_memory_keep,
)
from .context import (
    COMPACT_THRESHOLD,
    SESSION_ID,
    affection,
    last_memory_id,
    mcp,
    memory,
    proactive,
    router,
    ws,
)
from . import context
from .db import init_pool, close_pool, get_conn, get_conn_autocommit, get_pool
from .events import init as events_init, close as events_close
from .image_gen import needs_image
from .models import SessionState, new_id
from .personality import assemble_system_prompt, load_personality
from .push import send_push
from .routes import register_routes
from .helpers import (
    chunk_text as _chunk_text,
    fix_narration as _fix_narration,
    strip_actions_for_tts as _strip_actions_for_tts,
    wants_recall as _wants_recall,
    wants_mission_start as _wants_mission_start,
    wants_mission_cancel as _wants_mission_cancel,
    parse_interval_minutes as _parse_interval_minutes,
    create_conversation as _create_conversation,
    store_message as _store_message,
    RECALL_KEYWORDS, SAVE_KEYWORDS, DISCARD_KEYWORDS,
    MISSION_START_KEYWORDS, MISSION_CANCEL_KEYWORDS,
    TRIVIAL_PATTERNS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


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
    proactive.set_session_getter(lambda: memory.get_session(SESSION_ID))
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

# Register all HTTP routes from routes.py
register_routes(app)

# Serve Flutter PWA static files (mounted last so API routes take priority)
static_dir = Path("/app/static")
if static_dir.exists():
    app.mount("/app", StaticFiles(directory=str(static_dir), html=True), name="pwa")


# ── WebSocket ────────────────────────────────────────────────────────────────


async def _handle_tap_interact(user_id: str) -> None:
    """Handle tap interaction — deliver a short proactive comment."""
    if proactive and proactive._can_send():
        await proactive.trigger_tap()
    else:
        # Fallback: send a simple acknowledgment if proactive can't send
        await ws.send_proactive(user_id, "Hm? Right here, Commander.")


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

    # Restore mission timer from session if it was active before disconnect
    if session.mission_description and session.mission_interval and not proactive.mission_active:
        aff_state = await affection.get_state()
        proactive.set_affection_level(aff_state.level)
        proactive.start_mission(session.mission_description, session.mission_interval)
        logger.info(
            "Mission timer restored from session: every %d min",
            session.mission_interval,
        )

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

    # Track user activity for idle unload + romance window
    from .llm_router import mark_user_active
    mark_user_active()
    proactive.mark_user_messaged_today()

    start = time.monotonic()
    proactive.mark_responded()

    # ── Mission timer detection ───────────────────────────────────────────
    if _wants_mission_cancel(content):
        if proactive.mission_active:
            proactive.stop_mission()
            # Clear session mission state
            session.mission_description = None
            session.mission_interval = None
            session.mission_started_at = None
            await memory.save_session(SESSION_ID, session)
            logger.info("Mission timer cancelled by user")
        # Don't return — let the message go through to get an in-character response

    elif _wants_mission_start(content):
        interval = _parse_interval_minutes(content)
        # Use last few turns as mission context
        recent = session.turns[-4:] if session.turns else []
        mission_desc = " ".join(t.get("content", "")[:100] for t in recent if t.get("role") == "user")
        if not mission_desc:
            mission_desc = content

        aff_state = await affection.get_state()
        proactive.start_mission(mission_desc, interval)

        # Persist mission state in session so it survives Redis restores
        session.mission_description = mission_desc
        session.mission_interval = interval
        session.mission_started_at = datetime.now().isoformat()
        await memory.save_session(SESSION_ID, session)
        logger.info("Mission timer started: every %d min, desc='%s'", interval, mission_desc[:60])

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

    # Get active mission description for context injection
    mission_desc = None
    if proactive.mission_active and hasattr(proactive, '_mission_timer') and proactive._mission_timer:
        mission_desc = proactive._mission_timer.mission_description

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
        mission_description=mission_desc,
    )

    # Memory nudge — proactive past reference based on affection level
    nudge = await memory.get_memory_nudge(session.turn_count, aff_state.level)
    if nudge:
        system_prompt += f"\n\n{nudge}"

    # Image generation hint — keep text response minimal when an image is coming
    if needs_image(content):
        system_prompt += (
            "\n\nIMAGE GENERATION ACTIVE: An image is being generated for this request. "
            "Keep your text response to 1 SHORT sentence — just acknowledge the request. "
            "Do NOT narrate the process of taking a photo, using a data pad, or activating a camera. "
            "The image will appear separately. Example: '...Already done, Commander.' or "
            "'(I smirk) Patience.' Do NOT write more than 1-2 sentences."
        )

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
    if any(kw in content_lower for kw in SAVE_KEYWORDS) and context.last_memory_id:
        asyncio.create_task(do_memory_keep(context.last_memory_id, kept=True))
    elif any(kw in content_lower for kw in DISCARD_KEYWORDS) and context.last_memory_id:
        asyncio.create_task(do_memory_keep(context.last_memory_id, kept=False))

    # Track whether image gen was triggered (for extraction curation pass)
    image_triggered = False
    triggered_memory_id: str | None = None

    # Background tasks — only if main LLM succeeded (not a fallback error)
    if not response_text.startswith("Communications disrupted"):
        # Recall detection takes priority over new image generation
        if _wants_recall(content):
            logger.info("Memory recall triggered for: %s", content[:80])
            asyncio.create_task(background_recall(content, session, user_id))
        elif needs_image(content):
            logger.info("Image generation triggered for: %s", content[:80])
            # Build chat context from last few turns for scene-aware prompting
            recent_turns = session.turns[-6:]  # Last 3 exchanges
            chat_ctx = "\n".join(
                f"{t['role']}: {t['content'][:200]}" for t in recent_turns
            )
            image_triggered = True
            # Detect squad members mentioned for multi-character scenes
            from .image_gen import detect_squad_members
            mentioned_squad = detect_squad_members(f"{content} {chat_ctx}")
            asyncio.create_task(background_image_gen(
                content, chat_context=chat_ctx, squad_members=mentioned_squad,
            ))

    # Background: extract facts and create episodes
    # Skip extraction for trivial messages (no facts to extract, saves a full LLM round-trip)
    content_stripped = content.strip().lower().rstrip("!.?)")
    is_trivial = content_stripped in TRIVIAL_PATTERNS or (
        len(content_stripped) <= 5 and not image_triggered
    )

    if not is_trivial:
        async def _safe_extraction():
            try:
                await background_extraction(
                    content, response_text, session, user_id,
                    image_generated=image_triggered,
                )
            except Exception as e:
                import traceback
                logger.error("EXTRACTION CRASH: %s\n%s", e, traceback.format_exc())
        asyncio.create_task(_safe_extraction())
    else:
        logger.info("Trivial message, skipping extraction: %s", content[:40])

    # Background: compact session if turns exceed threshold
    if len(session.turns) >= COMPACT_THRESHOLD:
        asyncio.create_task(background_compaction(session))


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


# ── Re-exports for backward compatibility ────────────────────────────────────
# Tests and other modules that ``from app.main import ...`` will still work.

_fix_narration = _fix_narration
_enhance_image_prompt = None  # Lazy re-export below


def __getattr__(name: str):
    """Lazy re-exports for backward compatibility."""
    if name == "_enhance_image_prompt":
        from .helpers import enhance_image_prompt
        return enhance_image_prompt
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
