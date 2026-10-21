"""Post-reply task admission and ownership for one chat turn."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Coroutine
from dataclasses import dataclass
from typing import Any
import logging

from .models import SessionState
from .picture_curation import PictureCuration
from .ws_manager import WSManager

logger = logging.getLogger(__name__)

# Recent turns the picture's setting is read from (his last eight, with hers).
SCENE_TURNS = 16
SCENE_TURN_CHARS = 600


@dataclass
class ChatFollowups:
    ws: WSManager
    last_memory_id: Callable[[str], str | None]
    wants_recall: Callable[[str], bool]
    needs_image: Callable[[str], bool]
    keep_memory: Callable[..., Coroutine[Any, Any, None]]
    recall: Callable[[str, SessionState, str], Coroutine[Any, Any, None]]
    image: Callable[..., Coroutine[Any, Any, None]]
    extract: Callable[..., Coroutine[Any, Any, None]]
    compact: Callable[[SessionState, str], Coroutine[Any, Any, None]]
    save_keywords: Collection[str]
    discard_keywords: Collection[str]
    trivial_patterns: Collection[str]
    compact_threshold: int
    # Writes her keep/caption/category onto a saved picture (update_curation).
    curate: Callable[..., Coroutine[Any, Any, bool]] | None = None

    async def admit(
        self, content: str, response_text: str, session: SessionState, user_id: str
    ) -> None:
        # Commander save/discard overrides — operate on the most recent memory
        content_lower = content.lower()
        user_last_mem = self.last_memory_id(user_id)
        # Track post-response background tasks so a full user disconnect cancels
        # them — otherwise a stale task can wake after the user reconnects and
        # clobber the fresh session's write. The turn's extraction is the
        # exception (below): its session write is revision-guarded.
        if any(kw in content_lower for kw in self.save_keywords) and user_last_mem:
            self.ws.track_task(
                user_id, asyncio.create_task(self.keep_memory(user_last_mem, kept=True))
            )
        elif any(kw in content_lower for kw in self.discard_keywords) and user_last_mem:
            self.ws.track_task(
                user_id,
                asyncio.create_task(self.keep_memory(user_last_mem, kept=False)),
            )

        # Track whether image gen was triggered (for extraction curation pass)
        image_triggered = False
        picture: PictureCuration | None = None

        # Background tasks — the LLM answered (an llm_failed turn returned above).
        # Recall detection takes priority over new image generation.
        if self.wants_recall(content):
            logger.info("Memory recall triggered (%d chars)", len(content))
            self.ws.track_task(
                user_id, asyncio.create_task(self.recall(content, session, user_id))
            )
        elif self.needs_image(content):
            logger.info("Image generation triggered (%d chars)", len(content))
            # Build chat context from last few turns for scene-aware prompting
            recent_turns = session.turns[-6:]  # Last 3 exchanges
            chat_ctx = "\n".join(
                f"{t['role']}: {t['content'][:200]}" for t in recent_turns
            )
            image_triggered = True
            # Joins this picture to this turn's curation, whichever lands last.
            if self.curate is not None:
                picture = PictureCuration(self.curate, user_id)
            # Detect squad members mentioned for multi-character scenes
            from .image_gen import detect_squad_members

            mentioned_squad = detect_squad_members(f"{content} {chat_ctx}")
            # Who said what, so the picture's setting follows HIS words
            # (app/image_scene.py) — her replies only ever place her alone.
            scene_turns = [
                {
                    "role": str(t.get("role", "")),
                    "content": str(t.get("content", ""))[:SCENE_TURN_CHARS],
                }
                for t in session.turns[-SCENE_TURNS:]
            ]
            self.ws.track_task(
                user_id,
                asyncio.create_task(
                    self.image(
                        content,
                        chat_context=chat_ctx,
                        squad_members=mentioned_squad,
                        user_id=user_id,
                        recent_turns=scene_turns,
                        picture=picture,
                    )
                ),
                survive_disconnect=True,
            )

        # Background: extract facts and create episodes
        # Skip extraction for trivial messages (no facts to extract, saves a full LLM round-trip)
        content_stripped = content.strip().lower().rstrip("!.?)")
        is_trivial = content_stripped in self.trivial_patterns or (
            len(content_stripped) <= 5 and not image_triggered
        )

        if not is_trivial:
            extraction_snapshot = session.model_copy(deep=True)

            async def _safe_extraction() -> None:
                try:
                    await self.extract(
                        content,
                        response_text,
                        extraction_snapshot,
                        user_id,
                        image_generated=image_triggered,
                        picture=picture,
                    )
                except Exception as e:
                    import traceback

                    logger.error("EXTRACTION CRASH: %s\n%s", e, traceback.format_exc())

            # Survives his disconnect: closing the app right after her reply
            # cancelled it, and the turn lost its mood, heart rate, affection,
            # facts and recall memory. Its mood write checks the session
            # revision, so a newer turn always wins.
            self.ws.track_task(
                user_id,
                asyncio.create_task(_safe_extraction()),
                survive_disconnect=True,
            )
        else:
            logger.info("Trivial message, skipping extraction")

        # Background: compact session if turns exceed threshold
        if len(session.turns) >= self.compact_threshold:
            self.ws.track_task(
                user_id,
                asyncio.create_task(
                    self.compact(session.model_copy(deep=True), user_id)
                ),
            )
