"""Background tasks — extraction, compaction, image gen, recall.

These are all async functions launched via ``asyncio.create_task()``
from the WebSocket message handler in chat.py.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

from . import context
from . import memory_archive
from .context import ws, memory, router, affection, proactive, SESSION_ID, session_id
from .db import get_conn_autocommit
from .fact_extractor import create_episode_summary, extract_facts
from .helpers import enhance_image_prompt as _enhance_image_prompt
from .image_gen import build_prompt, generate_image, is_couple_scene, is_landscape
from .models import SessionState
from .personality import load_personality

logger = logging.getLogger(__name__)


async def background_extraction(
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
        aff_state_bg = await affection.get_state(user_id)
        result = await extract_facts(
            user_msg, assistant_msg,
            image_generated=image_generated,
            affection_level=aff_state_bg.level,
        )

        # Apply curation if image was generated and curation data came back
        # Resolve memory_id: prefer explicit arg, fall back to module-level last_memory_id
        # (image gen runs concurrently and sets it ~1-30s before extraction completes)
        curation_target = memory_id or (image_generated and context.last_memory_id) or None
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
        await memory.save_session(session_id(user_id), session)
        await ws.send_mood(user_id, mood)
        proactive.set_last_mood(mood)

        # Auto-start mission when mood hits battle_ready or vigilant (if was battle_ready)
        mission_moods = {"battle_ready"}
        # Vigilant triggers mission only if previous mood was battle_ready (mid-combat awareness)
        if mood == "vigilant" and proactive._last_mood == "battle_ready":
            mission_moods.add("vigilant")
        if mood in mission_moods and not proactive.mission_active:
            recent_text = " ".join(
                t.get("content", "")[:100] for t in session.turns[-4:]
                if t.get("role") == "user"
            ) or "Combat operation"
            proactive.start_mission(recent_text, interval_minutes=30)
            session.mission_description = recent_text
            session.mission_interval = 30
            session.mission_started_at = datetime.now().isoformat()
            await memory.save_session(session_id(user_id), session)
            logger.info("Mission auto-started from battle_ready mood: %s", recent_text[:60])

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

        # Adjust affection using the merged interaction classification (no separate LLM call)
        try:
            interaction = result.get("interaction", {})
            if isinstance(interaction, str):
                interaction = {"type": interaction, "intensity": 5}
            elif not isinstance(interaction, dict):
                interaction = {"type": "neutral", "intensity": 5}
            interaction_type = interaction.get("type", "neutral")
            intensity = max(1, min(10, int(interaction.get("intensity", 5))))
            aff_change = await affection.apply_classification(interaction_type, intensity, user_id)

            # Sync affection level to proactive engine
            proactive.set_affection_level(aff_change.new_level)

            # Send real-time affection update to UI
            await ws.send_affection(user_id,
                aff_change.new_score, aff_change.new_level,
                aff_change.new_level_name, aff_change.delta,
            )

            # Handle level transitions
            if aff_change.level_changed:
                await ws.send_affection_level_change(user_id,
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
                            await ws.send_proactive(user_id, line)
                            await asyncio.sleep(2)  # Pause between lines
                    else:
                        # Repeat level-up — just the short message
                        messages = aff_config.get("level_up_messages", {})
                        special_line = messages.get(aff_change.new_level)
                        if special_line:
                            await ws.send_proactive(user_id, special_line)
                else:
                    messages = aff_config.get("level_down_messages", {})
                    special_line = messages.get(aff_change.new_level)
                    if special_line:
                        await ws.send_proactive(user_id, special_line)

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
        import traceback
        logger.error("Background extraction failed: %s\n%s", e, traceback.format_exc())


async def background_compaction(session: SessionState, user_id: str = "jalsarraf") -> None:
    """Compact older session turns into a summary to reduce prefill tokens.

    Triggered when session.turns >= COMPACT_THRESHOLD. Summarizes the oldest
    turns via dolphin, keeps the last COMPACT_KEEP_RAW turns verbatim.
    """
    from .fact_extractor import compact_turns

    try:
        turns = session.turns
        if len(turns) < context.COMPACT_THRESHOLD:
            return

        # Split: compact the old turns, keep the recent ones raw
        old_turns = turns[:-context.COMPACT_KEEP_RAW]
        recent_turns = turns[-context.COMPACT_KEEP_RAW:]

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
        await memory.save_session(session_id(user_id), session)

        logger.info(
            "Session compacted: %d turns -> summary (%d chars) + %d raw turns",
            len(old_turns), len(summary), len(recent_turns),
        )
    except Exception as e:
        logger.error("Background compaction failed: %s", e)


async def background_image_gen(
    user_request: str,
    chat_context: str = "",
    squad_members: list[str] | None = None,
    user_id: str = "jalsarraf",
) -> None:
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
            await ws.send_thinking(user_id, "Warming up image systems, Commander. Stand by...")
        else:
            await ws.send_thinking(user_id, "Compiling tactical visualization, Commander...")

        # Use both the request AND recent chat for context-aware generation
        full_context = f"{chat_context}\n{user_request}" if chat_context else user_request
        couple = is_couple_scene(full_context)

        # Get affection level for mood-aware prompts
        aff_state = await affection.get_state(user_id)
        aff_level = aff_state.level

        scene_tags = _enhance_image_prompt(full_context, couple=couple)
        full_prompt = build_prompt(
            scene_tags, couple=couple, affection_level=aff_level,
            context=full_context, squad_members=squad_members,
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

            # Get current session state for archive metadata
            session_for_save = await memory.get_session(session_id(user_id))
            conv_id = session_for_save.conversation_id if session_for_save else "unknown"
            session_mood = session_for_save.mood if session_for_save else "composed"

            # Save to memory archive before sending to UI
            memory_id = await memory_archive.save_image(
                img_bytes, full_prompt, conv_id, session_mood, aff_level,
                user_id=user_id,
            )
            if memory_id:
                context.last_memory_id = memory_id
                logger.info("Image archived as memory %s", memory_id)

            img_b64 = b64.b64encode(img_bytes).decode()
            await ws.send(user_id, {"type": "image", "data": img_b64, "memory_id": memory_id})
            logger.info("Image sent to UI (%d bytes)", len(img_bytes))
        else:
            await ws.send_proactive(user_id, "...Visualization failed. Interference in the rendering pipeline. I'll try again later.")
    except Exception as e:
        logger.error("Background image gen failed: %s", e)


async def background_recall(content: str, session: SessionState, user_id: str) -> None:
    """Retrieve a memory from the archive and send it to the UI as a proactive message + image."""
    try:
        aff_state = await affection.get_state(user_id)
        mem = await memory_archive.recall_memory(content, session.mood, aff_state.level, user_id=user_id)
        if not mem:
            await ws.send_proactive(user_id, "...I searched through our records, but nothing matched. Perhaps we haven't made that memory yet, Commander.")
            return

        # Format the memory card
        from datetime import datetime, timezone
        mem_date = mem.get("created_at")
        if mem_date:
            if isinstance(mem_date, str):
                mem_date = datetime.fromisoformat(mem_date)
            if mem_date.tzinfo is None:
                mem_date = mem_date.replace(tzinfo=timezone.utc)
            days_ago = (datetime.now(timezone.utc) - mem_date).days
            if days_ago == 0:
                time_ref = "earlier today"
            elif days_ago == 1:
                time_ref = "yesterday"
            elif days_ago < 7:
                time_ref = f"{days_ago} days ago"
            else:
                time_ref = mem_date.strftime("%B %d")
        else:
            time_ref = ""

        category = mem.get("category", "")
        annotation = mem.get("annotation", "") or "A moment I've preserved."

        # Send formatted memory card
        card = f"[{category}]"
        if time_ref:
            card += f" — {time_ref}"
        card += f"\n\n{annotation}"
        await ws.send_proactive(user_id, card)

        img_bytes = await memory_archive.get_image_bytes(mem["id"], thumbnail=False)
        if img_bytes:
            import base64 as b64
            img_b64 = b64.b64encode(img_bytes).decode()
            await ws.send(user_id, {"type": "image", "data": img_b64, "memory_id": mem["id"]})
            logger.info("Recalled memory %s sent to UI", mem["id"])
    except Exception as e:
        logger.error("Background recall failed: %s", e)


async def do_memory_keep(memory_id: str, kept: bool) -> None:
    """Apply a commander save/discard override to a memory."""
    try:
        kept_by = "commander" if kept else "discarded"
        ok = await memory_archive.update_kept(memory_id, kept=kept, kept_by=kept_by)
        if ok:
            logger.info("Memory %s: kept=%s by commander", memory_id, kept)
    except Exception as e:
        logger.error("Memory keep/discard failed for %s: %s", memory_id, e)
