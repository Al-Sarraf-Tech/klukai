"""Post-response memory extraction with explicit service ownership.

The sequence preserves independent failure boundaries: malformed facts cannot
skip exchange persistence, and an optional enrichment cannot own the chat turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from typing import Any
import uuid

from .affection import AffectionManager, AffectionChange
from .extraction_llm import CallReport
from .heartbeat import (
    DEFAULT_INTENSITY,
    HIGH_INTENSITY_MOODS,
    coerce_intensity,
    heart_rate,
)
from .memory import MemoryManager
from .models import SessionState
from .picture_curation import PictureCuration
from .proactive import ProactiveEngine
from .ws_manager import WSManager

logger = logging.getLogger(__name__)


@dataclass
class ExtractionRuntime:
    memory: MemoryManager
    affection: AffectionManager
    proactive: ProactiveEngine
    ws: WSManager
    update_curation: Callable[..., Awaitable[bool]]
    extract_facts: Callable[..., Awaitable[dict[str, Any]]]
    create_summary: Callable[..., Awaitable[str | None]]
    apply_state: Callable[[SessionState, str, str], Awaitable[bool]]
    deliver_oath: Callable[[str], Awaitable[bool]]
    personality: Callable[[], dict[str, Any]]

    async def process(
        self,
        user_msg: str,
        assistant_msg: str,
        session: SessionState,
        user_id: str = "default",
        image_generated: bool = False,
        memory_id: str | None = None,
        picture: PictureCuration | None = None,
    ) -> None:
        """Background task: extract facts, adjust affection, and maybe create an episode.

        Delayed 3s so LLM calls don't compete with the main response stream.
        All calls are serialized (not parallel) to avoid queuing in LM Studio.

        Her mood, heart rate and affection land first, straight from the
        extraction result; the slower bookkeeping (curation, facts, promise
        and event detection with the Operation Brief, exchange storage and
        episode entries) follows, so a slow or unreachable model in that tail
        can never hold back what he sees of her this turn.
        """
        session = session.model_copy(deep=True)
        await asyncio.sleep(3)  # Let main response finish streaming first
        try:
            aff_state_bg = await self.affection.get_state(user_id)
            report = CallReport()
            result = await self.extract_facts(
                user_msg,
                assistant_msg,
                image_generated=image_generated,
                affection_level=aff_state_bg.level,
                report=report,
            )

            # Update mood in session + persist to PostgreSQL
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

            try:
                # Keeps the stale-revision guard: False when a newer turn exists.
                applied = await self.apply_state(session, mood, user_id)
            except Exception as e:
                # Session storage trouble must not cost the turn its facts,
                # promises or exchange record further down.
                logger.warning("Mood state not applied (%s)", type(e).__name__)
                applied = False
            await self.send_heartbeat(result, mood, user_id, applied, report)

            await self.adjust_affection(result, mood, user_id)

            # Her journal entry goes to THIS turn's picture: the explicit memory
            # id, or the turn's PictureCuration, which holds it until the
            # picture is saved (it is usually still rendering at this point).
            curation = result.get("memory_curation") if image_generated else None
            if isinstance(curation, dict):
                if memory_id:
                    try:
                        await self.update_curation(
                            memory_id, curation, aff_state_bg.level, user_id=user_id
                        )
                    except Exception as e:
                        logger.warning(
                            "Memory curation update failed for %s: %s", memory_id, e
                        )
                elif picture is not None:
                    await picture.curation_ready(curation, aff_state_bg.level)

            await self.store_facts(result, user_id)

            # Record "firsts" for anniversary tracking (scoped to user)
            if session.turn_count == 1:
                await self.proactive.record_first(user_id, "first_message")
                # Also record milestone in the fact store
                await self.memory.record_milestone("first_message", user_id=user_id)

            # Physical state: long conversation -> relaxed
            if session.turn_count > 0 and session.turn_count % 10 == 0:
                from .context import physical

                await physical.on_long_conversation(user_id)

            await self.store_commitments(user_msg, aff_state_bg.level, user_id)

            await self.store_exchange(
                result, mood, user_msg, assistant_msg, session, user_id
            )

        except Exception as e:
            import traceback

            logger.error(
                "Background extraction failed: %s\n%s", e, traceback.format_exc()
            )

    async def send_heartbeat(
        self,
        result: dict[str, Any],
        mood: str,
        user_id: str,
        applied: bool,
        report: CallReport,
    ) -> None:
        """Her heart rate for this turn, right after the mood frame.

        Also writes the turn's single extraction log line (provider, model,
        outcome, mood, intensity, bpm; never message content). A stale turn
        (its mood was not applied) sends no frame; a WS failure is logged and
        never skips affection or memory storage.
        """
        interaction = result.get("interaction")
        intensity = coerce_intensity(
            interaction.get("intensity")
            if isinstance(interaction, dict)
            else DEFAULT_INTENSITY
        )
        bpm = heart_rate(mood, intensity)
        logger.info(
            "Turn extraction: provider=%s model=%s outcome=%s mood=%s intensity=%d bpm=%d",
            report.provider,
            report.model or "-",
            report.outcome,
            mood,
            intensity,
            bpm,
        )
        if not applied:
            return
        try:
            await self.ws.send_heartbeat(user_id, bpm, mood, intensity)
        except Exception as e:
            logger.warning("Heartbeat frame failed: %s", type(e).__name__)

    async def store_facts(self, result: dict[str, Any], user_id: str) -> None:
        # Store new facts (scoped to user). LLM output is untrusted — skip any
        # non-dict / non-string entries instead of TypeError-ing the whole
        # extraction (which would also skip mood, affection, and exchange store).
        try:
            for fact in result.get("facts", []) or []:
                if not isinstance(fact, dict):
                    logger.warning("Skipping non-dict fact from extractor: %r", fact)
                    continue
                key, value = fact.get("key"), fact.get("value")
                if not isinstance(key, str) or not isinstance(value, str):
                    logger.warning(
                        "Skipping malformed fact (key/value types): %r", fact
                    )
                    continue
                if not key or not value:
                    continue
                await self.memory.set_relationship_fact(key, value, user_id=user_id)
        except Exception as e:
            logger.warning("Fact storage failed (continuing extraction): %s", e)

        # Store commander details as relationship facts (feature: she remembers)
        commander_details = result.get("commander_details", {})
        if isinstance(commander_details, dict):
            for detail_key, detail_val in commander_details.items():
                if detail_val and isinstance(detail_val, str):
                    await self.memory.set_relationship_fact(
                        f"commander_{detail_key}", detail_val, user_id=user_id
                    )

        # Store gift if detected (feature: comfort objects)
        gift_item = result.get("gift_item")
        if gift_item and isinstance(gift_item, str) and len(gift_item) > 1:
            await self.proactive.store_gift(user_id, gift_item)
            # Record as a "first" if it's the first gift
            await self.proactive.record_first(user_id, "first_gift")

        # Store inside joke / running reference if detected (feature: inside
        # jokes). Reuses the relationship-fact store under the rel:joke:*
        # namespace — no new table. Surfaced later via a per-message block.
        inside_joke = result.get("inside_joke")
        if isinstance(inside_joke, dict):
            jk_label = inside_joke.get("label")
            jk_note = inside_joke.get("note")
            if isinstance(jk_label, str) and isinstance(jk_note, str):
                await self.memory.set_inside_joke(jk_label, jk_note, user_id=user_id)

    async def store_commitments(self, user_msg: str, level: int, user_id: str) -> None:
        # Promises & gentle accountability: detect commitments the Commander
        # makes ("I'll…", "tomorrow I'll…") and schedule a caring follow-up.
        # A separate LLM call (the merged extraction prompt is already large),
        # fully fail-soft so it can never break the rest of extraction.
        upcoming_events: list[Any] = []
        try:
            from .fact_extractor import extract_promises
            from . import promises as promises_store

            promise_result = await extract_promises(user_msg, level)
            for commitment in promise_result.get("promises", []):
                await promises_store.store_promise(commitment, user_id=user_id)
            upcoming_events = promise_result.get("events") or []
        except Exception as e:
            logger.warning("Promise extraction failed: %s", e)

        # Operation Brief: the same call also returned his upcoming real-life
        # events. op_brief retires anything he says was cancelled, then plans a
        # brief / send-off / debrief chain on the deferred rail. Never raises.
        from . import op_brief

        await op_brief.on_turn(
            user_msg,
            upcoming_events,
            user_id=user_id,
            affection_level=level,
        )

    async def adjust_affection(
        self, result: dict[str, Any], mood: str, user_id: str
    ) -> None:
        # Adjust affection using the merged interaction classification (no separate LLM call)
        try:
            interaction = result.get("interaction", {})
            if isinstance(interaction, str):
                interaction = {"type": interaction, "intensity": 5}
            elif not isinstance(interaction, dict):
                interaction = {"type": "neutral", "intensity": 5}
            interaction_type = interaction.get("type", "neutral")
            intensity = coerce_intensity(interaction.get("intensity", 5))
            aff_change = await self.affection.apply_classification(
                interaction_type, intensity, user_id
            )

            # Heartbeat spike alert (app/heartbeat.py holds the BPM map).
            if mood in HIGH_INTENSITY_MOODS and intensity >= 7:
                bpm = HIGH_INTENSITY_MOODS[mood]
                await self.ws.send_heartbeat_spike(user_id, bpm, mood)
                logger.info(
                    "Heartbeat spike: %s bpm=%d intensity=%d", mood, bpm, intensity
                )

            # Sync affection level to proactive engine
            self.proactive.set_affection_level(aff_change.new_level, user_id=user_id)

            # Send real-time affection update to UI
            await self.ws.send_affection(
                user_id,
                aff_change.new_score,
                aff_change.new_level,
                aff_change.new_level_name,
                aff_change.delta,
            )

            # Handle level transitions
            if aff_change.level_changed:
                await self.announce_transition(aff_change, user_id)
        except Exception as e:
            logger.warning("Affection adjustment failed: %s", e)

    async def announce_transition(
        self, aff_change: AffectionChange, user_id: str
    ) -> None:
        await self.ws.send_affection_level_change(
            user_id,
            aff_change.new_level,
            aff_change.new_level_name,
            aff_change.level_direction,
        )

        personality = self.personality()
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
                oath_delivered = await self.deliver_oath(user_id)

            # Check for first-time milestone scene — scoped to THIS user
            # so one user's progress can't suppress (or steal) another
            # user's milestone. record_milestone is idempotent per user.
            milestone_key = f"affection_level_{aff_change.new_level}"
            is_new = await self.memory.record_milestone(milestone_key, user_id=user_id)

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
                    await self.ws.send_proactive(user_id, line)
                    await asyncio.sleep(2)  # Pause between lines
            else:
                # Repeat level-up — just the short message
                messages = aff_config.get("level_up_messages", {})
                special_line = messages.get(aff_change.new_level)
                if special_line:
                    await self.ws.send_proactive(user_id, special_line)
        else:
            messages = aff_config.get("level_down_messages", {})
            special_line = messages.get(aff_change.new_level)
            if special_line:
                await self.ws.send_proactive(user_id, special_line)

        logger.info(
            "Affection level %s: %d -> %d (%s)",
            aff_change.level_direction,
            aff_change.new_level - (1 if aff_change.level_direction == "up" else -1),
            aff_change.new_level,
            aff_change.new_level_name,
        )

    async def store_exchange(
        self,
        result: dict[str, Any],
        mood: str,
        user_msg: str,
        assistant_msg: str,
        session: SessionState,
        user_id: str,
    ) -> None:
        # Normalize topics once — extractor sometimes returns a bare string or null.
        raw_topics = result.get("topics", [])
        if isinstance(raw_topics, str):
            topics = [raw_topics] if raw_topics else []
        elif isinstance(raw_topics, list):
            topics = [t for t in raw_topics if isinstance(t, str)]
        else:
            topics = []

        # Store exchange in conversation memory for rich recall (scoped to user).
        # Isolated from fact-parsing so a malformed extractor payload never
        # silently drops the conversation from long-term self.memory.
        try:
            exchange_id = str(uuid.uuid4())
            await self.memory.store_exchange(
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

        # Create episode every 10 turns (scoped to user)
        if session.turn_count > 0 and session.turn_count % 10 == 0:
            try:
                summary = await self.create_summary(session.turns)
                if summary:
                    episode_id = str(uuid.uuid4())
                    await self.memory.store_episode(
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
