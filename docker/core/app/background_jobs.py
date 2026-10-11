"""On-demand background jobs — compaction, image gen, recall, memory keep.

Split out of ``background.py`` (which keeps the per-turn extraction pipeline)
so neither file trips the 500-line gate. Like extraction, these are async
functions launched via ``asyncio.create_task()`` from the WebSocket message
handler, and ``background.py`` re-exports them so callers and tests keep
importing from there.
"""

from __future__ import annotations

import asyncio
import logging

from . import context
from . import memory_archive
from .context import ws, memory, affection, proactive, session_id
from .helpers import enhance_image_prompt as _enhance_image_prompt
from .image_gen import build_prompt, generate_image, is_couple_scene, is_landscape
from .image_gen_constants import INTIMATE_MIN_LEVEL
from .models import SessionState

logger = logging.getLogger(__name__)


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

        # Split: compact the old turns, keep the recent ones raw. `prefix` is
        # a snapshot of exactly what gets compacted, compared against the live
        # list after the LLM call below.
        n_old = len(turns) - context.COMPACT_KEEP_RAW
        prefix = turns[:n_old]
        old_turns = list(prefix)

        # Build text to compact (include existing summary if any)
        if session.context_summary:
            old_turns = [{"role": "system", "content": f"[Previous summary: {session.context_summary}]"}] + old_turns

        summary = await compact_turns(old_turns)
        if not summary:
            logger.warning("Compaction returned empty summary, keeping raw turns")
            return

        # The chat kept going while the LLM summarised: replacing the list
        # with the stale "recent" slice would drop any turn appended since.
        # Delete the compacted prefix in place instead — and only if it is
        # still the same turns (another compaction or a session restore may
        # have changed the list underneath us; then this summary is stale).
        live = session.turns
        if live[:n_old] != prefix:
            logger.warning("Compaction aborted: session turns changed during summarisation")
            return
        session.context_summary = summary
        del live[:n_old]
        await memory.save_session(session_id(user_id), session)

        logger.info(
            "Session compacted: %d turns -> summary (%d chars) + %d raw turns",
            len(old_turns), len(summary), len(live),
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
        # Render what she is actually wearing right now (Her Day: today's pick,
        # his granted request, or block kit) — gated at the current level.
        from .her_day import render_costume
        costume = await render_costume(user_id, aff_level)
        full_prompt = build_prompt(
            scene_tags, couple=couple, affection_level=aff_level,
            context=full_context, squad_members=squad_members, costume=costume,
            request=user_request,
        )
        logger.info("Image prompt (aff=%d): %s", aff_level, full_prompt[:300])

        # Determine orientation
        if is_landscape(full_context):
            width, height = 1216, 832
        else:
            width, height = 832, 1216

        img_bytes = await generate_image(
            full_prompt, width=width, height=height,
            sfw=aff_level < INTIMATE_MIN_LEVEL,
        )
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
                context.set_last_memory_id(user_id, memory_id)
                logger.info("Image archived as memory %s", memory_id)

            img_b64 = b64.b64encode(img_bytes).decode()
            await ws.send(user_id, {"type": "image", "data": img_b64, "memory_id": memory_id})
            logger.info("Image sent to UI (%d bytes)", len(img_bytes))

            # Record first image milestone (both DB table and fact store)
            await proactive.record_first(user_id, "first_image")
            await memory.record_milestone("first_image", user_id=user_id)
        else:
            await ws.send_proactive(user_id, "...Visualization failed. Interference in the rendering pipeline. I'll try again later.", persist=False)
            await ws.send(user_id, {"type": "thinking", "content": ""})  # clear thinking bubble
    except Exception as e:
        logger.error("Background image gen failed: %s", e, exc_info=True)
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
        mem = await memory_archive.recall_memory(content, session.mood, aff_state.level, user_id=user_id)
        if not mem:
            await ws.send_proactive(user_id, "...I searched through our records, but nothing matched. Perhaps we haven't made that memory yet, Commander.", persist=False)
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

        img_bytes = await memory_archive.get_image_bytes(mem["id"], thumbnail=False, user_id=user_id)
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
