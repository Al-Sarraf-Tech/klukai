"""WebSocket handler and message processing.

The ``register_websocket(app)`` function attaches the ``/ws`` endpoint
and all supporting handlers (message, voice, tap-interact).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .agent_loop import AgentLoop
from .background import (
    background_compaction,
    background_extraction,
    background_image_gen,
    background_recall,
    do_memory_keep,
)
from . import context
from .context import (
    COMPACT_THRESHOLD,
    affection,
    mcp,
    memory,
    proactive,
    router,
    session_id,
    ws,
)
from .db import get_conn, get_conn_autocommit
from .helpers import (
    chunk_text as _chunk_text,
    fix_narration as _fix_narration,
    wants_recall as _wants_recall,
    wants_mission_start as _wants_mission_start,
    wants_mission_cancel as _wants_mission_cancel,
    parse_interval_minutes as _parse_interval_minutes,
    create_conversation as _create_conversation,
    store_message as _store_message,
    SAVE_KEYWORDS, DISCARD_KEYWORDS,
    TRIVIAL_PATTERNS,
)
from .image_gen import needs_image
from .models import SessionState, new_id
from .personality import assemble_system_prompt

logger = logging.getLogger(__name__)


# ── WebSocket ────────────────────────────────────────────────────────────────


REFLECTION_MIN_HOURS_AWAY = 8
REFLECTION_MAX_HOURS_AWAY = 72  # Over 3 days = too stale, skip


async def _maybe_reflect_on_return(user_id: str) -> None:
    """Greet the returning user with a reference to the last topic if away >8h.

    Pulled from companion_messages. Silent fail on any error — this is a
    best-effort nicety.
    """
    try:
        from datetime import datetime, timezone
        from .db import get_pool

        pool = get_pool()
        last_at = None
        recent_excerpts: list[tuple[str, str]] = []
        async with pool.connection() as conn:
            # Last message time
            row = await (await conn.execute(
                "SELECT MAX(created_at) FROM companion_messages WHERE user_id = %s",
                (user_id,),
            )).fetchone()
            if not row or not row[0]:
                return  # Brand new user — let them lead
            last_at = row[0]

            # Last 6 exchanges for context
            rows = await (await conn.execute(
                "SELECT role, content FROM companion_messages "
                "WHERE user_id = %s ORDER BY created_at DESC LIMIT 6",
                (user_id,),
            )).fetchall()
            recent_excerpts = [(r[0], r[1][:250]) for r in reversed(rows)]

        # Compute hours away
        now = datetime.now(timezone.utc)
        if last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)
        hours_away = (now - last_at).total_seconds() / 3600

        # Classify the greeting kind (dream / reflection / silent)
        from .character_behaviors import classify_return_greeting
        local_hour = datetime.now().hour
        kind = classify_return_greeting(
            hours_away=hours_away,
            local_hour=local_hour,
            min_hours=REFLECTION_MIN_HOURS_AWAY,
            max_hours=REFLECTION_MAX_HOURS_AWAY,
        )
        if kind == "silent":
            return
        if not recent_excerpts:
            return

        # Small delay so the UI settles before the greeting arrives
        import asyncio
        await asyncio.sleep(3)

        excerpt_text = "\n".join(f"{role}: {content}" for role, content in recent_excerpts)
        from .personality import load_personality, build_character_preamble
        p = load_personality()
        aff_state = await affection.get_state(user_id)
        system_prompt = build_character_preamble(p, aff_state.level)
        if kind == "dream":
            user_prompt = (
                f"Commander just reconnected in the morning after being away "
                f"about {int(hours_away)} hours. Open by telling them — warmly, "
                "briefly (2-3 sentences) — about a dream you had that was loosely "
                "inspired by your last conversation. First-person. Don't describe "
                "the dream as fiction — you lived it. No bullet points.\n\n"
                "Last conversation excerpt:\n" + excerpt_text
            )
        else:
            user_prompt = (
                f"Commander just reconnected after being away for about {int(hours_away)} hours. "
                "Greet them warmly but briefly (1-2 sentences). Reference something specific "
                "from the last exchanges below — a topic, a feeling, or something you'd been "
                "curious about. Do not use bullet points. Stay in first-person.\n\n"
                "Recent exchanges:\n" + excerpt_text
            )

        import os
        from .models import LLMConfig
        config = LLMConfig(
            provider="lmstudio",
            model="cognitivecomputations_dolphin-mistral-24b-venice-edition",
            base_url=os.environ.get("LM_STUDIO_URL", "http://192.168.50.2:1234"),
            temperature=0.9,
            max_tokens=180,
        )
        resp = await router.complete_local(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            config=config,
        )
        greeting = (
            resp.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        if not greeting or len(greeting) < 10:
            return

        # If this was a dream, persist it as a memory archive entry
        if kind == "dream":
            try:
                from . import dreams
                mood_now = getattr(aff_state, "mood", "tender") if hasattr(aff_state, "mood") else "tender"
                await dreams.save_dream(
                    dream_text=greeting,
                    user_id=user_id,
                    affection_level=aff_state.level,
                    mood=mood_now,
                )
            except Exception as e:
                logger.warning("Dream save skipped: %s", e)

        # Deliver via WS if still connected
        if ws.is_connected(user_id):
            await ws.send_proactive(user_id, greeting)
            logger.info(
                "Reflection-on-return sent to %s (away %dh kind=%s): %s",
                user_id, int(hours_away), kind, greeting[:60]
            )
    except Exception as e:
        logger.warning("Reflection-on-return failed: %s", e)


async def _handle_tap_interact(user_id: str) -> None:
    """Handle tap interaction — deliver a short proactive comment."""
    if proactive and proactive._can_send():
        await proactive.trigger_tap()
    else:
        # Fallback: send a simple acknowledgment if proactive can't send
        await ws.send_proactive(user_id, "Hm? Right here, Commander.")


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
            proactive.stop_mission(user_id=user_id)
            # Clear session mission state
            session.mission_description = None
            session.mission_interval = None
            session.mission_started_at = None
            await memory.save_session(session_id(user_id), session)
            logger.info("Mission timer cancelled by user")
        # Don't return — let the message go through to get an in-character response

    elif _wants_mission_start(content):
        interval = _parse_interval_minutes(content)
        # Use last few turns as mission context
        recent = session.turns[-4:] if session.turns else []
        mission_desc = " ".join(t.get("content", "")[:100] for t in recent if t.get("role") == "user")
        if not mission_desc:
            mission_desc = content

        aff_state = await affection.get_state(user_id)
        proactive.start_mission(mission_desc, interval)

        # Persist mission state in session so it survives Redis restores
        session.mission_description = mission_desc
        session.mission_interval = interval
        session.mission_started_at = datetime.now().isoformat()
        await memory.save_session(session_id(user_id), session)
        # Record first mission milestone
        await proactive.record_first(user_id, "first_mission")
        logger.info("Mission timer started: every %d min, desc='%s'", interval, mission_desc[:60])

    # Add user turn to session
    sid = session_id(user_id)
    session = await memory.add_turn(sid, "user", content, session)

    # Skip expensive memory recall for very short messages (hi, ok, yes, etc)
    is_short = len(content.strip()) <= 20
    if is_short:
        episode_memories: list[Any] = []
        rel_facts: dict[Any, Any] = {}
        recalled_exchanges: list[Any] = []
    else:
        episode_memories, rel_facts, recalled_exchanges = await memory.recall_for_prompt(
            content, user_id=user_id
        )

    # Get affection state for prompt modulation
    aff_state = await affection.get_state(user_id)

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

    # Detect if Commander is addressing a squad member
    from .helpers import detect_squad_address, detect_jealousy_trigger
    addressed_member = detect_squad_address(content)

    # Jealousy detection — Commander complimenting another T-Doll
    jealousy_target = detect_jealousy_trigger(content)

    # Physical state awareness
    from .context import physical
    phys_state, phys_detail = await physical.get_state(user_id)

    # Anniversary awareness
    anniversaries = await proactive.check_anniversaries(user_id)

    # Comfort objects for prompt context
    comfort_objects = await proactive.get_comfort_objects(user_id)

    # Crown jewel tribute — the Commander's most treasured words to Klukai.
    # Only surfaces in the prompt at affection level 4+ (per build_crown_jewel_block).
    from . import tributes
    crown_jewel = await tributes.get_crown_jewel(user_id)

    # Dream inquiry — if Commander asks about dreams, add context
    from .helpers import wants_dream_inquiry
    dream_hint = ""
    if wants_dream_inquiry(content):
        dream_hint = (
            "\n\nDREAM INQUIRY: The Commander is asking about your dreams. "
            "You DON'T remember your dreams (officially). Deny it with slight "
            "embarrassment: 'Dreams? I don't... dream. I'm a T-Doll.' "
            "But add a subtle tell — a pause, a look away, a faint blush. "
            "The truth is you DID dream about him. You just won't admit it."
        )

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
        addressed_member=addressed_member,
        jealousy_target=jealousy_target,
        physical_state=phys_state,
        physical_detail=phys_detail,
        anniversaries=anniversaries,
        comfort_objects=comfort_objects,
        crown_jewel=crown_jewel,
    )

    # Memory nudge — proactive past reference based on affection level
    nudge = await memory.get_memory_nudge(session.turn_count, aff_state.level, user_id=user_id)
    if nudge:
        system_prompt += f"\n\n{nudge}"

    # Dream inquiry hint
    if dream_hint:
        system_prompt += dream_hint

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
        agent_result = await agent.run(system_prompt, messages, user_id=user_id)

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
    session = await memory.add_turn(sid, "assistant", response_text, session)

    # Store messages in PostgreSQL + mark as read
    await _store_message(session.conversation_id, "user", content, model_name, user_id=user_id)
    try:
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "UPDATE companion_messages SET read_at = NOW() "
                "WHERE conversation_id = %s AND role = 'user' AND read_at IS NULL "
                "AND user_id = %s",
                (session.conversation_id, user_id),
            )
    except Exception as e:
        logger.warning("Failed to set read_at: %s", e)
    await ws.send(user_id, {"type": "read_receipt", "read_at": datetime.now().isoformat()})

    await _store_message(
        session.conversation_id, "assistant", response_text, model_name,
        latency_ms=latency_ms, user_id=user_id,
    )

    # Commander save/discard overrides — operate on the most recent memory
    content_lower = content.lower()
    user_last_mem = context.get_last_memory_id(user_id)
    if any(kw in content_lower for kw in SAVE_KEYWORDS) and user_last_mem:
        asyncio.create_task(do_memory_keep(user_last_mem, kept=True))
    elif any(kw in content_lower for kw in DISCARD_KEYWORDS) and user_last_mem:
        asyncio.create_task(do_memory_keep(user_last_mem, kept=False))

    # Track whether image gen was triggered (for extraction curation pass)
    image_triggered = False
    triggered_memory_id: str | None = None

    # Background tasks — only if main LLM succeeded (not a fallback error)
    if not response_text.startswith("Communications disrupted"):
        # Recall detection takes priority over new image generation
        if _wants_recall(content):
            logger.info("Memory recall triggered for: %s", content[:80])
            ws.track_task(user_id, asyncio.create_task(background_recall(content, session, user_id)))
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
            ws.track_task(user_id, asyncio.create_task(background_image_gen(
                content, chat_context=chat_ctx, squad_members=mentioned_squad,
                user_id=user_id,
            )))

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
        asyncio.create_task(background_compaction(session, user_id))


async def _handle_voice(audio_b64: str, session: SessionState, user_id: str = "default") -> None:
    """Process voice: STT -> text -> LLM -> TTS -> audio.

    Failure modes surface a UX signal — the user never gets ghosted.
    """
    voice_url = os.environ.get("VOICE_URL", "http://companion-voice:8301")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await ws.send_thinking(user_id, "Listening...")
            try:
                r = await client.post(f"{voice_url}/stt", json={"audio": audio_b64})
                r.raise_for_status()
                transcript = r.json().get("text", "")
            except Exception as stt_err:
                logger.error("STT failed: %s", stt_err)
                await ws.send_proactive(
                    user_id,
                    "...Voice link garbled, Commander — I couldn't make out the transmission. "
                    "Try again or switch to text.",
                )
                return

            if not transcript.strip():
                await ws.send_proactive(
                    user_id,
                    "...I heard nothing on the channel, Commander. Try again, closer to the mic.",
                )
                return

            # Process as text message (which streams the text response)
            await _handle_message(transcript, session, user_id)

            # Get the last assistant response for TTS
            session = await memory.get_session(session_id(user_id))
            if session and session.turns:
                last_turn = session.turns[-1]
                if last_turn["role"] == "assistant":
                    try:
                        r = await client.post(
                            f"{voice_url}/tts",
                            json={"text": last_turn["content"]},
                        )
                        if r.status_code == 200:
                            import base64
                            audio_out = base64.b64encode(r.content).decode()
                            await ws.send_voice(user_id, audio_out, final=True)
                        else:
                            logger.error("TTS HTTP %s: %s", r.status_code, r.text[:200])
                            await ws.send_proactive(
                                user_id,
                                "...The voice synth is offline, Commander — "
                                "I'm reading you in text instead.",
                            )
                    except Exception as tts_err:
                        logger.error("TTS failed: %s", tts_err)
                        await ws.send_proactive(
                            user_id,
                            "...Voice synth dropped, Commander — text reply only.",
                        )
    except Exception as e:
        logger.error("Voice processing failed: %s", e, exc_info=True)
        try:
            await ws.send_proactive(
                user_id,
                "...Voice channel broke entirely, Commander. Switching to text.",
            )
        except Exception:
            pass


def register_websocket(app: FastAPI) -> None:
    """Attach the WebSocket endpoint to *app*."""

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        # Authenticate via token query param
        token = websocket.query_params.get("token", "")
        if token:
            from .auth import get_user_from_token
            user_id = await get_user_from_token(token)
            if not user_id:
                await websocket.close(code=4001, reason="Invalid or expired token")
                return
        else:
            # Legacy fallback — reject unauthenticated connections
            await websocket.close(code=4001, reason="Authentication required")
            return

        await ws.connect(websocket, user_id)

        # Ensure session exists — always restore mood from PostgreSQL (source of truth)
        session_key = session_id(user_id)
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
            await _create_conversation(conv_id, user_id=user_id)
            logger.info("New session created with restored mood '%s' for user %s", restored_mood, user_id)
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
            aff_state = await affection.get_state(user_id)
            proactive.set_affection_level(aff_state.level)
            proactive.start_mission(session.mission_description, session.mission_interval)
            logger.info(
                "Mission timer restored from session: every %d min",
                session.mission_interval,
            )

        # Reflection-on-return: if user was away >8h, greet them referencing
        # the last topic. Runs in background so it never blocks the connect.
        import asyncio as _asyncio
        ws.track_task(user_id, _asyncio.create_task(_maybe_reflect_on_return(user_id)))

        try:
            while True:
                data = await ws.receive(user_id)
                if data is None:
                    break

                msg_type = data.get("type")

                if msg_type == "message":
                    content = data.get("content", "")
                    if isinstance(content, str):
                        content = content[:4000]  # Input length limit
                    await _handle_message(content, session, user_id)
                elif msg_type == "typing":
                    pass
                elif msg_type == "voice_end":
                    audio = data.get("audio")
                    if audio:
                        await _handle_voice(audio, session, user_id)
                elif msg_type == "tap_interact":
                    await _handle_tap_interact(user_id)
        except WebSocketDisconnect:
            pass
        finally:
            await ws.disconnect(user_id, websocket)
