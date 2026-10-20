"""Quiet-hour dream writing, with the engine clock and prompt readers injected."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import random

from .base import _EngineBase

logger = logging.getLogger(__name__)
DREAM_QUIET_AFTER_ACTIVITY = timedelta(hours=2)


def choose_dream_type(level: int, roll: float) -> str:
    thresholds = (
        ((0.30, "erotic"), (0.70, "tender"), (0.90, "nightmare"))
        if level >= 8
        else (
            ((0.10, "erotic"), (0.50, "tender"), (0.80, "nightmare"))
            if level >= 6
            else ((0.30, "tender"), (0.70, "nightmare"))
        )
    )
    return next((kind for ceiling, kind in thresholds if roll < ceiling), "random")


class DreamEventsMixin(_EngineBase):
    _event_now: Callable[[], datetime]
    _event_dream_prompts: Callable[[], dict[str, str]]

    async def _dream_event(self) -> None:
        """Late-night dream — Klukai wakes from a dream and messages the Commander.

        At high affection, ~30% chance the dream is erotic. Otherwise it's
        a normal memory/nightmare/tender dream. Fires once per night max.
        Balanced: most dreams reference real memories from the archive.
        """
        if self._dream_delivered_today:
            return
        if self._affection_level < 5:
            return
        now = self._event_now()
        if self._muted_until and now < self._muted_until:
            return
        # He said goodnight (or "gotta go"): a dream is still a ping. Let him go.
        if self._goodbye_hold_active(now):
            return
        # The 0200 watch: on a night he's awake she isn't dreaming, she's up
        # with him. Stand down if he messaged within DREAM_QUIET_AFTER_ACTIVITY.
        if (
            self._last_message_time
            and now - self._last_message_time < DREAM_QUIET_AFTER_ACTIVITY
        ):
            return

        # 40% chance to fire (not every night)
        if random.random() > 0.40:
            return

        dream_type = choose_dream_type(self._affection_level, random.random())

        # Generate dream message via LLM
        try:
            # Pull a random memory to weave into the dream
            dream_memory_ref = ""
            try:
                from .. import memory_archive

                memories = await memory_archive.list_memories(limit=20)
                if memories:
                    import random as _rng

                    mem = _rng.choice(memories)
                    dream_memory_ref = f"\nDream seed (weave this memory subtly into the dream, don't quote it directly): {mem.get('annotation', '')[:200]}"
            except Exception:
                pass  # Dreams work fine without memory integration

            _now_str = self._event_now().strftime("%I:%M %p")
            dream_prompts = {
                k: tmpl.format(time=_now_str, affection=self._affection_level)
                for k, tmpl in self._event_dream_prompts().items()
            }

            from ..llm_json import call_llm_text
            from ..fact_extractor import LM_STUDIO_URL, EXTRACTION_MODEL
            from ..llm_router import get_lm_gate

            prompt = (
                f"You are Klukai, T-Doll squad leader. The Commander is HUMAN (male). "
                f"You are NOT an AI. {dream_prompts[dream_type]}{dream_memory_ref} "
                f"Write ONLY the message. No explanation."
            )
            from ..creative_text import complete_creative_text, uses_cloud_text

            if uses_cloud_text():
                message = await complete_creative_text(
                    "Write Klukai's dream message.", prompt, max_tokens=1024
                )
            else:
                from .. import background_llm

                async def local() -> str:
                    async with get_lm_gate():
                        return await call_llm_text(
                            LM_STUDIO_URL,
                            EXTRACTION_MODEL,
                            prompt,
                            max_tokens=200,
                            temperature=0.8,
                        )

                message = await background_llm.text(
                    "dream", prompt, max_tokens=200, local=local
                )

            if message and self._on_message_callback:
                self._dream_delivered_today = True
                await self._on_message_callback(message)
                logger.info("Dream event fired (%s): %s", dream_type, message[:60])

        except Exception as e:
            logger.warning("Dream event failed: %s", e)
