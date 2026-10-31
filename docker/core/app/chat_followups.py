"""Post-reply task admission and ownership for one chat turn."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Coroutine
from dataclasses import dataclass, field
from typing import Any
import logging

from .extraction_llm import ExtractionCancelled
from .models import SessionState
from .picture_curation import PictureCuration
from .ws_manager import WSManager

logger = logging.getLogger(__name__)

# Recent turns the picture's setting is read from (his last eight, with hers).
SCENE_TURNS = 16
SCENE_TURN_CHARS = 600
# Wait until he has been quiet this long before spending a background call.
EXTRACTION_IDLE_S = 4.0
# A preempted extraction goes back on the queue, but every try reserves
# another attempt in the daily extraction ledger. Give up after this many.
EXTRACTION_MAX_TRIES = 3
# Waiting turns per user. When a long chat outruns extraction, the oldest
# waiting turn is dropped (and logged) rather than holding every snapshot.
EXTRACTION_MAX_PENDING = 12


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

    # Render-only contextual wording; never replaces the stored user turn.
    image_request: str | None = None
    extraction_idle_s: float = EXTRACTION_IDLE_S
    extraction_max_tries: int = EXTRACTION_MAX_TRIES
    extraction_max_pending: int = EXTRACTION_MAX_PENDING
    # Per user: waiting jobs as [job, tries], oldest first.
    _extract_queue: dict[str, list[list[Any]]] = field(default_factory=dict)
    _extract_gen: dict[str, int] = field(default_factory=dict)
    _extract_runner: dict[str, asyncio.Task[None]] = field(default_factory=dict)

    def _queue_extract(
        self, user_id: str, job: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        """Run extraction after a quiet gap, one job at a time per user.

        A new turn restarts the quiet gap. A preempted job goes back to the
        front of the queue, at most ``extraction_max_tries`` times in all.
        """
        pending = self._extract_queue.setdefault(user_id, [])
        pending.append([job, 0])
        while len(pending) > self.extraction_max_pending:
            pending.pop(0)
            logger.warning(
                "Extraction backlog full for this user; dropped the oldest turn"
            )
        self._extract_gen[user_id] = self._extract_gen.get(user_id, 0) + 1
        runner = self._extract_runner.get(user_id)
        if runner is not None and not runner.done():
            return  # the running loop picks the job up after the next quiet gap

        async def run() -> None:
            try:
                while True:
                    generation = self._extract_gen.get(user_id)
                    await asyncio.sleep(self.extraction_idle_s)
                    if self._extract_gen.get(user_id) != generation:
                        continue  # he spoke again: wait for a fresh quiet gap
                    jobs = self._extract_queue.get(user_id)
                    if not jobs:
                        return
                    entry = jobs.pop(0)
                    entry[1] += 1
                    try:
                        await entry[0]()
                    except ExtractionCancelled:
                        if entry[1] < self.extraction_max_tries:
                            jobs.insert(0, entry)
                        else:
                            logger.warning(
                                "Extraction preempted %d times; skipping this turn",
                                entry[1],
                            )
            finally:
                if self._extract_runner.get(user_id) is task:
                    del self._extract_runner[user_id]
                if not self._extract_queue.get(user_id):
                    self._extract_queue.pop(user_id, None)

        task = asyncio.create_task(run())
        self._extract_runner[user_id] = task
        self.ws.track_task(user_id, task, survive_disconnect=True)

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
                        self.image_request
                        if self.image_request is not None
                        else content,
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
                except ExtractionCancelled:
                    raise
                except Exception as e:
                    import traceback

                    logger.error("EXTRACTION CRASH: %s\n%s", e, traceback.format_exc())

            # Idle a few seconds so a quick follow-up does not start the call.
            # A recall that cancels it puts the same job back on the queue.
            self._queue_extract(user_id, _safe_extraction)
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
