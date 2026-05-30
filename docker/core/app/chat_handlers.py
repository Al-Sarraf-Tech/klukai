"""Chat handlers — _maybe_reflect_on_return + _handle_message.

Extracted from app/chat.py for file-size hygiene (S+ Phase 2 §6.1).
Public surface preserved: callers still `from app.chat import _handle_message`
via re-export at the bottom of chat.py.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any


from . import context
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
    affection,
    mcp,
    memory,
    proactive,
    router,
    session_id,
    ws,
)
from .db import get_conn_autocommit
from .helpers import (
    SAVE_KEYWORDS,
    DISCARD_KEYWORDS,
    TRIVIAL_PATTERNS,
    chunk_text as _chunk_text,
    fix_narration as _fix_narration,
    wants_recall as _wants_recall,
    wants_mission_start as _wants_mission_start,
    wants_mission_cancel as _wants_mission_cancel,
    parse_interval_minutes as _parse_interval_minutes,
    store_message as _store_message,
)
from .image_gen import needs_image
from .models import SessionState, new_id
from .personality import (
    assemble_system_prompt,
    build_growth_arc_block,
    build_inside_jokes_block,
    load_personality,
)

logger = logging.getLogger(__name__)

from app.reflect_helpers import (  # noqa: F401,E402
    _maybe_oath_on_connect,
    _maybe_reflect_on_return,
)

REFLECTION_MIN_HOURS_AWAY = 8
REFLECTION_MAX_HOURS_AWAY = 72



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
        try:
            episode_memories, rel_facts, recalled_exchanges = await memory.recall_for_prompt(
                content, user_id=user_id
            )
        except Exception as e:
            # Defense in depth: recall_for_prompt already fails open, but never let
            # a memory-layer surprise tear down the chat/WS — degrade to no recalled
            # context and keep replying.
            logger.warning("recall_for_prompt failed, continuing with empty context: %s", e)
            episode_memories, rel_facts, recalled_exchanges = [], {}, []

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

    # Inside jokes / running references (feature: inside jokes). Surfaced as a
    # per-message block below — NOT folded into assemble_system_prompt — so the
    # golden system-prompt snapshots stay stable. Skipped for very short msgs.
    inside_jokes = [] if is_short else await memory.get_inside_jokes(user_id)

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

    # Inside jokes + growth arc — appended here (NOT in assemble_system_prompt)
    # so the persistent prompt's golden snapshots stay stable. Both degrade to
    # empty strings when not applicable (affection too low, no data, off-cadence).
    _p = load_personality()
    _jk_cfg = _p.get("inside_jokes", {})
    if _jk_cfg.get("enabled", True):
        jokes_block = build_inside_jokes_block(
            inside_jokes, aff_state.level,
            max_surfaced=_jk_cfg.get("max_surfaced", 2),
            min_affection_level=_jk_cfg.get("min_affection_level", 3),
        )
        if jokes_block:
            system_prompt += f"\n\n{jokes_block}"
    growth_block = build_growth_arc_block(_p, aff_state.level, session.turn_count)
    if growth_block:
        system_prompt += f"\n\n{growth_block}"

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
    # Track all post-response background tasks so a full user disconnect cancels
    # them — otherwise a stale task (extraction/compaction) can wake after the
    # user reconnects and clobber the fresh session's write.
    if any(kw in content_lower for kw in SAVE_KEYWORDS) and user_last_mem:
        ws.track_task(user_id, asyncio.create_task(do_memory_keep(user_last_mem, kept=True)))
    elif any(kw in content_lower for kw in DISCARD_KEYWORDS) and user_last_mem:
        ws.track_task(user_id, asyncio.create_task(do_memory_keep(user_last_mem, kept=False)))

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
        ws.track_task(user_id, asyncio.create_task(_safe_extraction()))
    else:
        logger.info("Trivial message, skipping extraction: %s", content[:40])

    # Background: compact session if turns exceed threshold
    if len(session.turns) >= COMPACT_THRESHOLD:
        ws.track_task(user_id, asyncio.create_task(background_compaction(session, user_id)))
