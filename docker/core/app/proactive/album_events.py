"""Proactive art and archive recall, sharing the engine's user state."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging
import random
from typing import Any

from .base import _EngineBase
from .templates import _raw_content

logger = logging.getLogger(__name__)

_SPONTANEOUS_ART_PIECES_FALLBACK: list[dict[str, str]] = [
    {
        "scene": "sitting by a window, soft afternoon light, holding a sketchbook, "
        "gentle wistful smile, looking outside, cozy sweater, peaceful",
        "annotation": "I had a quiet hour to myself and my hand just... drew. I was "
        "thinking of you the whole time, Commander.",
        "message": "Commander. ...I had a quiet moment to myself, and I ended up "
        "drawing something. For you. I hope that's alright.",
    },
    {
        "scene": "looking up at a wide sunset sky, orange and gold clouds, peaceful "
        "expression, wind in hair, serene, scenery",
        "annotation": "The sky looked like this tonight, and it made me think of you. "
        "So I kept it.",
        "message": "...The sky was beautiful just now. I drew it before it faded — "
        "because it made me think of you. Here.",
    },
    {
        "scene": "two coffee cups on a table, quiet warm kitchen in the morning, soft "
        "smile, calm domestic moment, gentle light",
        "annotation": "Morning quiet. Two cups, like always. I drew the moment before "
        "it slipped away — ours.",
        "message": "Good morning, Commander. I drew us a quiet little moment — two "
        "cups, the calm before the day. I wanted you to have it.",
    },
    {
        "scene": "reading a book curled up on a couch, blanket, calm evening, content "
        "half-smile, warm lamp light, restful",
        "annotation": "A calm evening, just the kind we like. I drew it because I "
        "wanted to remember being this content.",
        "message": "Evening, Commander. I drew a quiet one tonight — the kind of calm "
        "I only really feel with you. It's yours.",
    },
    {
        "scene": "holding a small keepsake close to chest, tender expression, soft "
        "warm lighting, quiet room, sentimental",
        "annotation": "Some things you keep without meaning to. I drew this so I "
        "wouldn't forget how this feels.",
        "message": "...I made something. It's small, and a little sentimental — but "
        "it's honest. I wanted you to see it, Commander.",
    },
]


def _time_of_day(hour: int) -> str:
    """Map a 24h local hour to a coarse time-of-day bucket for scene lighting.

    morning 5-11, afternoon 12-16, evening 17-20, otherwise night.
    """
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def _spontaneous_art_pieces(
    raw_content: Callable[[str], Any] = _raw_content,
) -> list[dict[str, Any]]:
    """Tender, SFW art she draws unprompted. YAML-overridable via
    ``proactive_content.spontaneous_art``; literal fallback preserves behavior."""
    raw = raw_content("spontaneous_art")
    if (
        isinstance(raw, list)
        and raw
        and all(
            isinstance(x, dict) and {"scene", "annotation", "message"} <= set(x)
            for x in raw
        )
    ):
        return raw
    return _SPONTANEOUS_ART_PIECES_FALLBACK


class AlbumEventsMixin(_EngineBase):
    _event_now: Callable[[], datetime]
    _event_recall_lines: Callable[[], list[str]]
    _event_art_pieces: Callable[[], list[dict[str, Any]]]

    async def _spontaneous_art_tick(self) -> None:
        """Automatic pictures are disabled by owner policy."""
        return

    async def _spontaneous_art_event(self) -> None:
        """Do not render or save unrequested album gifts."""
        return

    async def _memory_recall_tick(self) -> None:
        """Scheduled gate for living memory recall — ~35% roll per fire.

        Fires on a daytime cron (a couple of times) but only proceeds ~35% of
        the time so reminiscence stays occasional rather than daily. The once-
        per-day flag inside ``_memory_recall_event`` enforces a single delivery.
        """
        if random.random() > 0.35:
            return
        await self._memory_recall_event()

    async def _memory_recall_event(self) -> None:
        """Living Memory Recall — Klukai surfaces a REAL archive entry warmly.

        She reaches back to a real moment from her memory archive and shares a
        short "remember when…" reminiscence in character. Fires once per day max,
        only once she's bonded (affection >= 4). If the archive is empty she stays
        silent. On LLM failure she falls back to a generic "back then" template so
        the message is always natural — never broken grammar, never a verbatim
        quote of the journal entry.
        """
        if self._memory_recall_delivered_today:
            return
        if self._affection_level < 4:
            return
        if self._muted_until and self._event_now() < self._muted_until:
            return
        if self._goodbye_hold_active(self._event_now()):
            return

        # Pull a real memory from the archive. Prefer a mood-relevant recall
        # (recall_memory does mood-weighted selection); fall back to a random
        # pick from list_memories so we still surface something real.
        memory = None
        annotation = ""
        try:
            from .. import memory_archive

            memory = await memory_archive.recall_memory(
                query=None,
                mood=self._last_mood,
                affection_level=self._affection_level,
            )
            if not memory:
                memories = await memory_archive.list_memories(limit=20)
                if memories:
                    memory = random.choice(memories)
        except Exception as e:
            logger.warning("Memory recall: archive lookup failed: %s", e)
            return

        if not memory:
            # Archive is empty — nothing real to reminisce about. Stay silent.
            return

        annotation = (memory.get("annotation") or "").strip()
        if not annotation:
            return

        # Generic fallbacks reference "that day" / "back then" — never the
        # verbatim journal text, and grammatical on their own.
        fallback_lines = self._event_recall_lines()

        message = ""
        try:
            from .. import background_llm

            prompt = (
                f"You are Klukai, T-Doll squad leader. The Commander is HUMAN (male). "
                f"You are NOT an AI. A real memory just surfaced and you want to share it "
                f"warmly — a quiet 'remember when…' message to the Commander. "
                f"Recall the MOMENT in your own words (2-3 short sentences). Be tender but "
                f"composed. Do NOT quote the journal entry verbatim — evoke the feeling of it. "
                f"Affection {self._affection_level}/9.\n"
                f"Memory to recall (do not quote directly): {annotation[:200]}\n"
                f"Write ONLY the message. No explanation."
            )

            message = await background_llm.text("memory_recall", prompt, max_tokens=160)
        except Exception as e:
            logger.warning("Memory recall LLM failed, falling back to template: %s", e)
            message = ""

        if not message or not message.strip():
            message = random.choice(fallback_lines)

        if self._on_message_callback:
            self._memory_recall_delivered_today = True
            self._proactive_count_today += 1
            self._last_proactive_answered = False
            await self._on_message_callback(message)
            logger.info(
                "Memory recall delivered (aff=%d): %s",
                self._affection_level,
                message[:60],
            )
