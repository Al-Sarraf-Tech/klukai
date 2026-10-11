"""Background tasks — extraction, compaction, image gen, recall.

These are all async functions launched via ``asyncio.create_task()``
from the WebSocket message handler in chat.py. The per-turn extraction
pipeline lives here; compaction, image gen, recall and the memory
keep/discard override live in ``background_jobs`` and are re-exported below.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

from . import context
from . import memory_archive
from .context import ws, memory, affection, proactive, session_id
from .db import get_conn_autocommit
from .fact_extractor import create_episode_summary, extract_facts
from .models import SessionState
from .personality import load_personality

# Compaction, image gen, recall and the keep/discard override live in
# background_jobs (split for the 500-line gate); re-exported so chat_handlers
# and the tests keep importing them from here.
from .background_jobs import (  # noqa: F401
    background_compaction,
    background_image_gen,
    background_recall,
    do_memory_keep,
)

logger = logging.getLogger(__name__)


async def maybe_deliver_oath(user_id: str) -> bool:
    """Deliver the one-time level-9 "Oath Fulfilled" capstone if it has never
    fired for this user. Guarded by companion_firsts('oath_fulfilled') so it
    fires EXACTLY once ever — safe to call both on the level-up-to-9 transition
    and on connect (for a Commander who reached lv9 before this feature existed).

    The once-ever "first" is only consumed when there is actually a scene to
    deliver, so a missing/empty config can't silently burn the trigger.
    Returns True iff the scene was delivered this call.
    """
    oath_scene = load_personality().get("affection", {}).get("oath_fulfilled_scene", [])
    if not oath_scene:
        return False
    if not await proactive.record_first(user_id, "oath_fulfilled"):
        return False  # already fired once — never repeat
    for line in oath_scene:
        await ws.send_proactive(user_id, line)
        await asyncio.sleep(3)  # weighty pacing
    logger.info("Oath Fulfilled capstone delivered (once-ever) for %s", user_id)
    return True


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
    The phases below run in a fixed order; each fail-soft step logs and lets
    the rest continue so one malformed extractor field never costs the mood,
    the affection update or the exchange store.
    """
    await asyncio.sleep(3)  # Let main response finish streaming first
    try:
        aff_state_bg = await affection.get_state(user_id)
        result = await extract_facts(
            user_msg, assistant_msg,
            image_generated=image_generated,
            affection_level=aff_state_bg.level,
        )

        await _apply_memory_curation(result, user_id, aff_state_bg.level, image_generated, memory_id)
        await _store_extracted_facts(result, user_id)
        await _store_gift_and_inside_joke(result, user_id)

        upcoming_events = await _store_promises(user_msg, aff_state_bg.level, user_id)
        # Operation Brief: the same call also returned his upcoming real-life
        # events. op_brief retires anything he says was cancelled, then plans a
        # brief / send-off / debrief chain on the deferred rail. Never raises.
        from . import op_brief
        await op_brief.on_turn(
            user_msg, upcoming_events, user_id=user_id,
            affection_level=aff_state_bg.level,
        )

        mood = _resolve_mood(result)
        await _apply_mood(mood, session, user_id)
        await _apply_affection(result, mood, user_id)

        topics = _normalize_topics(result)
        await _store_exchange(user_msg, assistant_msg, result, topics, mood, session, user_id)
        await _maybe_store_episode(session, topics, mood, user_id)
    except Exception as e:
        import traceback
        logger.error("Background extraction failed: %s\n%s", e, traceback.format_exc())


async def _apply_memory_curation(
    result: dict, user_id: str, affection_level: int, image_generated: bool, memory_id: str | None,
) -> None:
    """Apply the extractor's curation to the image this turn produced, if any."""
    # Resolve memory_id: prefer explicit arg, fall back to module-level last_memory_id
    # (image gen runs concurrently and sets it ~1-30s before extraction completes)
    curation_target = memory_id or (image_generated and context.get_last_memory_id(user_id)) or None
    if image_generated and curation_target and "memory_curation" in result:
        try:
            await memory_archive.update_curation(
                curation_target, result["memory_curation"], affection_level
            )
        except Exception as e:
            logger.warning("Memory curation update failed for %s: %s", curation_target, e)


async def _store_extracted_facts(result: dict, user_id: str) -> None:
    """Store new facts and commander details as relationship facts (scoped to user)."""
    # LLM output is untrusted — skip any non-dict / non-string entries instead
    # of TypeError-ing the whole extraction (which would also skip mood,
    # affection, and exchange store).
    try:
        for fact in result.get("facts", []) or []:
            if not isinstance(fact, dict):
                logger.warning("Skipping non-dict fact from extractor: %r", fact)
                continue
            key, value = fact.get("key"), fact.get("value")
            if not isinstance(key, str) or not isinstance(value, str):
                logger.warning("Skipping malformed fact (key/value types): %r", fact)
                continue
            if not key or not value:
                continue
            await memory.set_relationship_fact(key, value, user_id=user_id)
    except Exception as e:
        logger.warning("Fact storage failed (continuing extraction): %s", e)

    # Store commander details as relationship facts (feature: she remembers)
    commander_details = result.get("commander_details", {})
    if isinstance(commander_details, dict):
        for detail_key, detail_val in commander_details.items():
            if detail_val and isinstance(detail_val, str):
                await memory.set_relationship_fact(
                    f"commander_{detail_key}", detail_val, user_id=user_id
                )


async def _store_gift_and_inside_joke(result: dict, user_id: str) -> None:
    """Comfort objects and running references the extractor noticed."""
    # Store gift if detected (feature: comfort objects)
    gift_item = result.get("gift_item")
    if gift_item and isinstance(gift_item, str) and len(gift_item) > 1:
        await proactive.store_gift(user_id, gift_item)
        # Record as a "first" if it's the first gift
        await proactive.record_first(user_id, "first_gift")

    # Store inside joke / running reference if detected (feature: inside
    # jokes). Reuses the relationship-fact store under the rel:joke:*
    # namespace — no new table. Surfaced later via a per-message block.
    inside_joke = result.get("inside_joke")
    if isinstance(inside_joke, dict):
        jk_label = inside_joke.get("label")
        jk_note = inside_joke.get("note")
        if isinstance(jk_label, str) and isinstance(jk_note, str):
            await memory.set_inside_joke(jk_label, jk_note, user_id=user_id)


async def _store_promises(user_msg: str, affection_level: int, user_id: str) -> list:
    """Promises & gentle accountability: detect commitments the Commander
    makes ("I'll…", "tomorrow I'll…") and schedule a caring follow-up.

    A separate LLM call (the merged extraction prompt is already large),
    fully fail-soft so it can never break the rest of extraction. Returns the
    upcoming real-life events the same call found, for the Operation Brief.
    """
    try:
        from .fact_extractor import extract_promises
        from . import promises as promises_store
        promise_result = await extract_promises(user_msg, affection_level)
        for commitment in promise_result.get("promises", []):
            await promises_store.store_promise(commitment, user_id=user_id)
        return promise_result.get("events") or []
    except Exception as e:
        logger.warning("Promise extraction failed: %s", e)
        return []


def _resolve_mood(result: dict) -> str:
    """The LLM-detected mood, nudged one step toward the user's sentiment."""
    mood = result.get("mood", "composed")

    # Mood contagion: nudge Klukai's mood toward user's sentiment.
    # Small effect — the LLM-detected mood is the primary signal; contagion
    # is a one-step bounded adjustment that never jumps more than one register.
    try:
        from .character_behaviors import nudge_mood, interaction_to_sentiment
        sentiment = interaction_to_sentiment(result.get("interaction"))
        if sentiment:
            mood = nudge_mood(mood, sentiment)
    except Exception as e:
        logger.debug("Mood contagion skipped: %s", e)
    return mood


async def _apply_mood(mood: str, session: SessionState, user_id: str) -> None:
    """Update mood in the session, the UI and PostgreSQL; record firsts; maybe
    auto-start a mission."""
    # The mood she was in BEFORE this turn — read it before set_last_mood
    # overwrites it, or the vigilant-after-battle_ready check below only
    # ever sees the new mood and can never fire.
    prev_mood = proactive._last_mood

    session.mood = mood
    await memory.save_session(session_id(user_id), session)
    await ws.send_mood(user_id, mood)
    proactive.set_last_mood(mood)

    # Record "firsts" for anniversary tracking (scoped to user)
    if session.turn_count == 1:
        await proactive.record_first(user_id, "first_message")
        # Also record milestone in the fact store
        await memory.record_milestone("first_message", user_id=user_id)

    # Physical state: long conversation -> relaxed
    if session.turn_count > 0 and session.turn_count % 10 == 0:
        from .context import physical
        await physical.on_long_conversation(user_id)

    await _maybe_auto_start_mission(mood, prev_mood, session, user_id)

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


async def _maybe_auto_start_mission(
    mood: str, prev_mood: str | None, session: SessionState, user_id: str,
) -> None:
    """Auto-start mission when mood hits battle_ready or vigilant (if was battle_ready)."""
    mission_moods = {"battle_ready"}
    # Vigilant triggers mission only if previous mood was battle_ready (mid-combat awareness)
    if mood == "vigilant" and prev_mood == "battle_ready":
        mission_moods.add("vigilant")
    if mood in mission_moods and not proactive.mission_active:
        recent_text = " ".join(
            t.get("content", "")[:100] for t in session.turns[-4:]
            if t.get("role") == "user"
        ) or "Combat operation"
        # Nobody asked for this mission, so it stands down on its own: without
        # the ceiling it ran until the cancel phrase or a restart, ticking
        # through the night.
        from .proactive.mission import AUTO_MISSION_MAX_MINUTES
        proactive.start_mission(
            recent_text, interval_minutes=30, max_duration_minutes=AUTO_MISSION_MAX_MINUTES,
        )
        session.mission_description = recent_text
        session.mission_interval = 30
        session.mission_started_at = datetime.now().isoformat()
        await memory.save_session(session_id(user_id), session)
        logger.info("Mission auto-started from battle_ready mood: %s", recent_text[:60])


# Moods whose high-intensity turns register as a heartbeat spike in the UI.
HIGH_INTENSITY_MOODS = {
    "passionate": 165, "terrified": 175, "panicked": 180,
    "desperate": 170, "furious": 160, "adrenaline": 155,
    "yearning": 150, "infatuated": 152,
}


async def _apply_affection(result: dict, mood: str, user_id: str) -> None:
    """Adjust affection using the merged interaction classification (no
    separate LLM call), push the update to the UI, and handle level changes."""
    try:
        interaction = result.get("interaction", {})
        if isinstance(interaction, str):
            interaction = {"type": interaction, "intensity": 5}
        elif not isinstance(interaction, dict):
            interaction = {"type": "neutral", "intensity": 5}
        interaction_type = interaction.get("type", "neutral")
        intensity = max(1, min(10, int(interaction.get("intensity", 5))))
        aff_change = await affection.apply_classification(interaction_type, intensity, user_id)

        # Heartbeat spike detection (feature: heartbeat spike alerts)
        if mood in HIGH_INTENSITY_MOODS and intensity >= 7:
            bpm = HIGH_INTENSITY_MOODS[mood]
            await ws.send_heartbeat_spike(user_id, bpm, mood)
            logger.info("Heartbeat spike: %s bpm=%d intensity=%d", mood, bpm, intensity)

        # Sync affection level to proactive engine
        proactive.set_affection_level(aff_change.new_level)

        # Send real-time affection update to UI
        await ws.send_affection(user_id,
            aff_change.new_score, aff_change.new_level,
            aff_change.new_level_name, aff_change.delta,
        )

        # Handle level transitions
        if aff_change.level_changed:
            await _announce_level_change(aff_change, user_id)
    except Exception as e:
        logger.warning("Affection adjustment failed: %s", e)


async def _announce_level_change(aff_change, user_id: str) -> None:
    """Tell the UI, then deliver the matching scene or line for the new level."""
    await ws.send_affection_level_change(user_id,
        aff_change.new_level, aff_change.new_level_name,
        aff_change.level_direction,
    )

    personality = load_personality()
    aff_config = personality.get("affection", {})

    if aff_change.level_direction == "up":
        # ── PRIMARY: Level-9 "Oath Fulfilled" capstone ──────────
        # The first time affection reaches level 9, fire the one-time
        # oath scene (indigo_oath / wedding imagery, "Every day, I
        # choose you again."). Guarded by companion_firsts so it can
        # NEVER repeat — a later return to lv9 falls through to the
        # short level_up_messages[9] line. We deliver it as a
        # distinctive proactive scene because no UI costume-swap WS
        # event exists (verified ws_manager.py).
        oath_delivered = False
        if aff_change.new_level == 9:
            oath_delivered = await maybe_deliver_oath(user_id)

        # Check for first-time milestone scene — scoped to THIS user
        # so one user's progress can't suppress (or steal) another
        # user's milestone. record_milestone is idempotent per user.
        milestone_key = f"affection_level_{aff_change.new_level}"
        is_new = await memory.record_milestone(milestone_key, user_id=user_id)

        if oath_delivered:
            # The oath scene IS the level-9 moment — the milestone is
            # already recorded above, so just suppress the ordinary
            # scene/message this turn.
            pass
        elif is_new:
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


def _normalize_topics(result: dict) -> list[str]:
    """Normalize topics once — extractor sometimes returns a bare string or null."""
    raw_topics = result.get("topics", [])
    if isinstance(raw_topics, str):
        return [raw_topics] if raw_topics else []
    if isinstance(raw_topics, list):
        return [t for t in raw_topics if isinstance(t, str)]
    return []


async def _store_exchange(
    user_msg: str, assistant_msg: str, result: dict, topics: list[str],
    mood: str, session: SessionState, user_id: str,
) -> None:
    """Store exchange in conversation memory for rich recall (scoped to user).

    Isolated from fact-parsing so a malformed extractor payload never
    silently drops the conversation from long-term memory.
    """
    try:
        exchange_id = str(uuid.uuid4())
        await memory.store_exchange(
            exchange_id=exchange_id,
            user_content=user_msg,
            assistant_content=assistant_msg,
            topics=topics,
            mood=mood if isinstance(mood, str) else "composed",
            importance=0.7 if result.get("should_remember") else 0.4,
            conversation_id=session.conversation_id,
            user_id=user_id,
        )
    except Exception as e:
        logger.warning("Exchange storage failed: %s", e)


async def _maybe_store_episode(
    session: SessionState, topics: list[str], mood: str, user_id: str,
) -> None:
    """Create episode every 10 turns (scoped to user)."""
    if not (session.turn_count > 0 and session.turn_count % 10 == 0):
        return
    try:
        summary = await create_episode_summary(session.turns)
        if summary:
            episode_id = str(uuid.uuid4())
            await memory.store_episode(
                episode_id=episode_id,
                summary=summary,
                keywords=topics,
                emotion_tags=[mood] if isinstance(mood, str) else [],
                importance=0.5,
                conversation_id=session.conversation_id,
                user_id=user_id,
            )
            logger.info("Episode stored: %s", summary[:80])
    except Exception as e:
        logger.warning("Episode creation failed: %s", e)
