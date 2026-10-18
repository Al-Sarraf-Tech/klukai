"""Background tasks — extraction, compaction, image gen, recall.

These are all async functions launched via ``asyncio.create_task()``
from the WebSocket message handler in chat.py.
"""

from __future__ import annotations
from typing import Any


import asyncio
import logging
from datetime import datetime

from . import context
from . import memory_archive
from .context import ws, memory, affection, proactive, session_id
from .db import get_conn_autocommit
from .fact_extractor import create_episode_summary, extract_facts
from .helpers import enhance_image_prompt as _enhance_image_prompt
from .image_gen import build_prompt, generate_image, is_couple_scene, is_landscape
from .image_gen_constants import INTIMATE_MIN_LEVEL
from .character_scenes import commit_oath_scene, oath_lines
from .models import SessionState
from .proactive.mission_timer import AUTO_MISSION_MAX_MINUTES
from .memory_extraction import ExtractionRuntime
from .personality import load_personality

logger = logging.getLogger(__name__)


async def maybe_deliver_oath(user_id: str) -> bool:
    """Deliver the one-time level-9 "Oath Fulfilled" capstone if it has never
    fired for this user. The full scene and companion_firsts('oath_fulfilled')
    commit together, so a disconnect during pacing cannot consume the trigger
    or leave a partial scene. Safe to call on the level-up-to-9 transition and
    on connect (for a Commander who reached lv9 before this feature existed).

    Missing or invalid scene config does not touch the trigger. An existing
    trigger is left exactly as found. Returns True only after the history
    commit; live lines are then paced with persistence disabled.
    """
    scene = oath_lines(
        load_personality().get("affection", {}).get("oath_fulfilled_scene")
    )
    if not scene:
        return False
    if not await commit_oath_scene(user_id, scene):
        return False
    for line in scene:
        await ws.send_proactive(user_id, line, persist=False)
        await asyncio.sleep(3)  # weighty pacing; history is already complete
    logger.info("Oath Fulfilled capstone delivered (once-ever) for %s", user_id)
    return True


async def _apply_extracted_state(
    snapshot: SessionState, mood: str, user_id: str
) -> bool:
    """Atomically guard mood, mission and their persistent/timer/WS effects."""
    key = session_id(user_id)
    async with memory.session_owner(key):
        session = await memory.get_session(key)
        if (
            session is None
            or session.conversation_id != snapshot.conversation_id
            or session.revision != snapshot.revision
        ):
            return False
        # The mood she was in BEFORE this turn — read it before set_last_mood
        # overwrites it, or the vigilant-after-battle_ready check below only
        # ever sees the new mood and can never fire.
        prev_mood = proactive._moods.get(user_id, "composed")

        session.mood = mood

        start_mission = False
        # Auto-start mission when mood hits battle_ready or vigilant (if was battle_ready)
        mission_moods = {"battle_ready"}
        # Vigilant triggers mission only if previous mood was battle_ready (mid-combat awareness)
        if mood == "vigilant" and prev_mood == "battle_ready":
            mission_moods.add("vigilant")
        if mood in mission_moods and not proactive.get_mission_timer(user_id):
            recent_text = (
                " ".join(
                    t.get("content", "")[:100]
                    for t in session.turns[-4:]
                    if t.get("role") == "user"
                )
                or "Combat operation"
            )
            start_mission = True
            session.mission_description = recent_text
            session.mission_interval = 30
            session.mission_started_at = datetime.now().isoformat()
            session.mission_max_minutes = AUTO_MISSION_MAX_MINUTES
            logger.info(
                "Mission auto-started from battle_ready mood: %s", recent_text[:60]
            )

        await memory.save_session(key, session)
        proactive.set_last_mood(mood, user_id=user_id)
        if start_mission and session.mission_description is not None:
            # Nobody asked for this mission, so it stands down on its own:
            # without the ceiling it ran until the cancel phrase or a restart,
            # ticking through the night.
            proactive.start_mission(
                session.mission_description,
                interval_minutes=30,
                user_id=user_id,
                max_duration_minutes=AUTO_MISSION_MAX_MINUTES,
            )
        await ws.send_mood(user_id, mood)

        # Persist mood so it survives session expiry
        try:
            async with get_conn_autocommit() as conn:
                await conn.execute(
                    "INSERT INTO companion_persistent_state (user_id, mood, last_conversation_id, updated_at) "
                    "VALUES (%s, %s, %s, NOW()) "
                    "ON CONFLICT (user_id) DO UPDATE SET mood = %s, last_conversation_id = %s, updated_at = NOW()",
                    (
                        user_id,
                        mood,
                        session.conversation_id,
                        mood,
                        session.conversation_id,
                    ),
                )
        except Exception as e:
            logger.warning("Failed to persist mood: %s", e)

        return True


async def background_extraction(
    user_msg: str,
    assistant_msg: str,
    session: SessionState,
    user_id: str = "default",
    image_generated: bool = False,
    memory_id: str | None = None,
) -> None:
    runtime = ExtractionRuntime(
        memory,
        affection,
        proactive,
        ws,
        context.get_last_memory_id,
        memory_archive.update_curation,
        extract_facts,
        create_episode_summary,
        _apply_extracted_state,
        maybe_deliver_oath,
        load_personality,
    )
    await runtime.process(
        user_msg, assistant_msg, session, user_id, image_generated, memory_id
    )


async def background_compaction(
    session: SessionState, user_id: str = "jalsarraf"
) -> None:
    """Compact older session turns into a summary to reduce prefill tokens.

    Triggered when session.turns >= COMPACT_THRESHOLD. Summarizes the oldest
    turns via dolphin, keeps the last COMPACT_KEEP_RAW turns verbatim.
    """
    from .fact_extractor import compact_turns

    session = session.model_copy(deep=True)
    try:
        turns = session.turns
        if len(turns) < context.COMPACT_THRESHOLD:
            return

        # Split: compact the old turns, keep the recent ones raw. `prefix` is
        # a snapshot of exactly what gets compacted, compared against the live
        # list after the LLM call below.
        n_old = len(turns) - context.COMPACT_KEEP_RAW
        prefix = turns[:n_old]
        old_turns = list(prefix)

        # Build text to compact (include existing summary if any)
        if session.context_summary:
            old_turns = [
                {
                    "role": "system",
                    "content": f"[Previous summary: {session.context_summary}]",
                }
            ] + old_turns

        summary = await compact_turns(old_turns)
        if not summary:
            logger.warning("Compaction returned empty summary, keeping raw turns")
            return

        key = session_id(user_id)
        async with memory.session_owner(key):
            live = await memory.get_session(key)
            if (
                live is None
                or live.conversation_id != session.conversation_id
                or live.context_summary != session.context_summary
                or live.turns[:n_old] != prefix
            ):
                logger.info(
                    "Compaction skipped: stored prefix, summary or conversation changed"
                )
                return
            live.context_summary = summary
            del live.turns[:n_old]
            await memory.save_session(key, live)
            logger.info(
                "Session compacted: %d old turns, %d raw turns remain",
                n_old,
                len(live.turns),
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
    from .image_provider import cloud_images_ready, selected_provider
    from .agy_image_provider import uses_agy_images

    try:
        provider = selected_provider()
        use_agy = provider == "agy" or uses_agy_images(user_id)
        if use_agy:
            ready = True  # The host worker fails visibly; never probe the GPU.
        elif provider == "openai":
            if not await asyncio.to_thread(cloud_images_ready):
                await ws.send_proactive(
                    user_id,
                    "...Visualization is unavailable right now, Commander.",
                    persist=False,
                )
                await ws.send(user_id, {"type": "thinking", "content": ""})
                return
            ready = True
        elif provider == "comfyui":
            # Only local rendering waits for VRAM and probes the GPU gateway.
            logger.info("Image gen: waiting 1s for VRAM...")
            await asyncio.sleep(1)
            ready = await check_comfyui_ready()
        else:
            await ws.send_proactive(
                user_id,
                "...Visualization is unavailable right now, Commander.",
                persist=False,
            )
            await ws.send(user_id, {"type": "thinking", "content": ""})
            return
        logger.info("Image gen: starting")
        if not ready:
            await ws.send_thinking(
                user_id, "Warming up image systems, Commander. Stand by..."
            )
        else:
            await ws.send_thinking(
                user_id, "Compiling tactical visualization, Commander..."
            )

        # Agy receives only the explicit request and local appearance tags.
        # Squad hints supplied by chat_handlers may include old conversations.
        full_context = (
            user_request
            if use_agy
            else (f"{chat_context}\n{user_request}" if chat_context else user_request)
        )
        if use_agy:
            from .image_gen import detect_squad_members

            squad_members = detect_squad_members(user_request)
        couple = is_couple_scene(full_context)

        # Get affection level for mood-aware prompts
        aff_state = await affection.get_state(user_id)
        aff_level = aff_state.level

        scene_tags = _enhance_image_prompt(full_context, couple=couple)
        # Render what she is actually wearing right now (Her Day: today's pick,
        # his granted request, or block kit) — gated at the current level.
        from .her_day import render_costume

        costume = await render_costume(user_id, aff_level)
        full_prompt = build_prompt(
            scene_tags,
            couple=couple,
            affection_level=aff_level,
            context=full_context,
            squad_members=squad_members,
            costume=costume,
            request=user_request,
        )
        logger.info("Image composition prepared (aff=%d)", aff_level)

        # Determine orientation
        if is_landscape(full_context):
            width, height = 1216, 832
        else:
            width, height = 832, 1216

        outcome = None
        if use_agy:
            from .account_images import explicit_request_id, fulfill_account_image
            from .image_provider import cloud_prompt

            full_prompt = f"{user_request}\n\n{cloud_prompt(full_prompt, sfw=aff_level < INTIMATE_MIN_LEVEL)}"
            # The id is the explicit sentence, so a repeat finds this journal row.
            request_id = explicit_request_id(
                user_id, user_request, width, height, couple
            )
            accepted_session = await memory.get_session(session_id(user_id))
            archive_context = {
                "conversation_id": accepted_session.conversation_id
                if accepted_session
                else "unknown",
                "mood": accepted_session.mood if accepted_session else "composed",
                "affection_level": aff_level,
            }
            outcome = await fulfill_account_image(
                user_id,
                full_prompt,
                width=width,
                height=height,
                include_commander=couple,
                request_id=request_id,
                archive_context=archive_context,
            )
            img_bytes = outcome.get("image")
        else:
            img_bytes = await generate_image(
                full_prompt,
                width=width,
                height=height,
                sfw=aff_level < INTIMATE_MIN_LEVEL,
            )
        if img_bytes:
            await _deliver_generated_image(
                user_id, img_bytes, full_prompt, aff_level, outcome
            )
        else:
            await ws.send_proactive(
                user_id,
                "...Visualization failed. Interference in the rendering pipeline. I'll try again later.",
                persist=False,
            )
            await ws.send(
                user_id, {"type": "thinking", "content": ""}
            )  # clear thinking bubble
    except Exception as e:
        logger.error("Background image gen failed (%s)", type(e).__name__)
        try:
            # Mirror the empty-bytes fallback so the user never gets ghosted —
            # always clear the thinking bubble and surface a graceful message.
            await ws.send_proactive(
                user_id,
                "...The rendering pipeline broke mid-frame, Commander. "
                "I'll attempt the visual again later.",
                persist=False,
            )
            await ws.send(user_id, {"type": "thinking", "content": ""})
        except Exception:
            pass


async def background_recall(content: str, session: SessionState, user_id: str) -> None:
    """Retrieve a memory from the archive and send it to the UI as a proactive message + image."""
    try:
        aff_state = await affection.get_state(user_id)
        mem = await memory_archive.recall_memory(
            content, session.mood, aff_state.level, user_id=user_id
        )
        if not mem:
            await ws.send_proactive(
                user_id,
                "...I searched through our records, but nothing matched. Perhaps we haven't made that memory yet, Commander.",
                persist=False,
            )
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

        img_bytes = await memory_archive.get_image_bytes(
            mem["id"], thumbnail=False, user_id=user_id
        )
        if img_bytes:
            import base64 as b64

            img_b64 = b64.b64encode(img_bytes).decode()
            await ws.send(
                user_id, {"type": "image", "data": img_b64, "memory_id": mem["id"]}
            )
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


async def _deliver_generated_image(
    user_id: str,
    img_bytes: bytes,
    full_prompt: str,
    aff_level: int,
    outcome: dict[str, Any] | None,
) -> None:
    from .account_images import complete_archive
    import base64 as b64

    # Get current session state for archive metadata
    session_for_save = await memory.get_session(session_id(user_id))
    conv_id = session_for_save.conversation_id if session_for_save else "unknown"
    session_mood = session_for_save.mood if session_for_save else "composed"

    if outcome is not None and outcome.get("needs_archive"):
        metadata = outcome["archive_metadata"]
        memory_id = await memory_archive.save_image(
            img_bytes,
            metadata["prompt"],
            metadata["conversation_id"],
            metadata["mood"],
            metadata["affection_level"],
            user_id=user_id,
            memory_id=outcome["request_id"],
        )
        outcome = await complete_archive(
            outcome["request_id"], memory_id, img_bytes, user_id
        )
    elif outcome is None:
        memory_id = await memory_archive.save_image(
            img_bytes,
            full_prompt,
            conv_id,
            session_mood,
            aff_level,
            user_id=user_id,
        )
    else:
        memory_id = outcome.get("memory_id") if outcome.get("archived") else None
    archived = bool(memory_id)
    if memory_id is not None:
        context.set_last_memory_id(user_id, memory_id)
        logger.info("Image archived as memory %s", memory_id)

    img_b64 = b64.b64encode(img_bytes).decode()
    frame = {"type": "image", "data": img_b64, "archived": archived}
    if archived:
        frame["memory_id"] = memory_id
    if outcome is not None:
        frame["request_id"] = outcome["request_id"]
    elif memory_id:
        frame["memory_id"] = memory_id
    await ws.send(user_id, frame)
    logger.info("Image sent to UI (%d bytes)", len(img_bytes))

    # Record first image milestone (both DB table and fact store)
    await proactive.record_first(user_id, "first_image")
    await memory.record_milestone("first_image", user_id=user_id)
