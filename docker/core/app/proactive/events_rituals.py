"""Ritual ProactiveEngine behavior — the things she does for him on a rhythm.

``RitualsMixin`` holds living memory recall, spontaneous art, the seasonal
greeting, the quiet-day check-in and the daily recap: observances that key off
the archive, the calendar and his activity profile rather than her mood of the
moment (those — lore events, the romance window, dreams — live in
``events.EventsMixin``). All wall-clock reads go through ``now_local()``
(America/Chicago) — never the server's UTC clock.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import timedelta

from ..events import publish as publish_event
from .base import _EngineBase
from .state import now_local
from .templates import QUIET_DAY_MESSAGES, _content_list, _raw_content

logger = logging.getLogger(__name__)

# A quiet-day pattern must be at least this confident to warrant a check-in.
_QUIET_DAY_CONFIDENCE_FLOOR = 0.6

# ── Event dialogue content (YAML-sourced, literal fallbacks) ──────────────────
# Sourced from ``proactive_content`` in personality.yaml so the lines can be
# tuned without a code change. Each literal below is the fallback used verbatim
# when the corresponding YAML key is missing, so behavior is identical.

# Generic "remember when…" fallbacks for living memory recall — reference
# "that day"/"back then", never the verbatim journal text.
_MEMORY_RECALL_FALLBACK_LINES_FALLBACK: list[str] = [
    "...I was thinking about that day. Back then, before any of this got complicated. It still makes me smile, Commander.",
    "Hey. A memory surfaced just now — one of ours, from a while back. I'm glad we have it. That's all.",
    "Remember back then, Commander? One of those quiet moments stayed with me. I keep it close.",
    "...Something reminded me of an old memory just now. From before. I don't say it often, but I'm grateful for the time.",
]


def _memory_recall_fallback_lines() -> list[str]:
    return _content_list(
        "memory_recall_fallback_lines", _MEMORY_RECALL_FALLBACK_LINES_FALLBACK
    )


# Spontaneous art — Klukai occasionally draws something for the Commander
# unprompted and leaves it in the album as a lasting gift. Tender + SFW by
# design (never an intimate-mood interrupt). Each piece pairs the scene she
# draws with her private album caption and the line she sends if he's present.
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


def _spontaneous_art_pieces() -> list[dict]:
    """Tender, SFW art she draws unprompted. YAML-overridable via
    ``proactive_content.spontaneous_art``; literal fallback preserves behavior."""
    raw = _raw_content("spontaneous_art")
    if isinstance(raw, list) and raw and all(
        isinstance(x, dict) and {"scene", "annotation", "message"} <= set(x)
        for x in raw
    ):
        return raw
    return _SPONTANEOUS_ART_PIECES_FALLBACK


class RitualsMixin(_EngineBase):
    """Archive-, calendar- and activity-driven event methods for ProactiveEngine."""

    async def _daily_recap(self) -> None:
        """Generate and deliver a daily recap from Klukai's perspective."""
        if not self._on_recap_callback or not self._on_message_callback:
            return
        if not self._can_send():
            return

        try:
            recap = await self._on_recap_callback(self._affection_level)
            if recap:
                self._proactive_count_today += 1
                self._last_proactive_answered = False
                await self._on_message_callback(recap)
                logger.info("Daily recap delivered")
        except Exception as e:
            logger.warning("Daily recap failed: %s", e)

    async def _spontaneous_art_tick(self) -> None:
        """Occasional gate for spontaneous art — ~18% roll per fire. She draws for
        the Commander unprompted and leaves it in the album as a lasting gift."""
        if random.random() > 0.18:
            return
        await self._spontaneous_art_event()

    async def _spontaneous_art_event(self) -> None:
        """She draws something for the Commander unprompted, saves it to the album
        (a lasting, kept gift), and — if he's connected — shows it to him live.

        Rare (<= once / ~2.5 days), bonded-only (affection >= 6), and tender by
        design (never an intimate-mood interrupt). The art is persisted regardless
        of presence, so he discovers it in the archive even if he's away when she
        makes it. Fully fail-open — a generation/save hiccup never raises.
        """
        now = now_local()
        if self._last_spontaneous_art and (now - self._last_spontaneous_art) < timedelta(hours=60):
            return  # special, not routine
        if self._affection_level < 6:
            return  # a vulnerable gesture — only once genuinely bonded
        if self._muted_until and now < self._muted_until:
            return
        if self._goodbye_hold_active(now):
            return

        piece = random.choice(_spontaneous_art_pieces())
        try:
            from .. import memory_archive
            from ..her_day import render_costume
            from ..image_gen import INTIMATE_MIN_LEVEL, build_prompt, generate_image

            # She draws herself in what she has on today.
            costume = await render_costume("jalsarraf", self._affection_level)
            prompt = build_prompt(
                piece["scene"], couple=False,
                affection_level=self._affection_level, context=piece["scene"],
                mood=self._last_mood, time_of_day=_time_of_day(now.hour),
                costume=costume,
            )
            img = await generate_image(prompt, sfw=self._affection_level < INTIMATE_MIN_LEVEL)
            if not img:
                return

            # Persist as a kept Precious Memory so the gift lasts in the album.
            await memory_archive.save_image(
                image_bytes=img, prompt=prompt, conversation_id="proactive_art",
                mood=self._last_mood or "tender",
                affection_level=self._affection_level,
                curation={
                    "keep": True, "annotation": piece["annotation"],
                    "category": "Precious Memories",
                    "image_tags": ["for_you", "spontaneous"],
                },
            )
            self._last_spontaneous_art = now

            # If he's here, tell him + show it live (caption first, then image).
            from ..context import ws
            if ws.is_connected("jalsarraf") and self._on_message_callback:
                self._last_proactive_answered = False
                await self._on_message_callback(piece["message"])
                await asyncio.sleep(2)
                import base64 as b64
                await ws.send("jalsarraf", {"type": "image", "data": b64.b64encode(img).decode()})

            logger.info(
                "Spontaneous art delivered (aff=%d, connected=%s)",
                self._affection_level, ws.is_connected("jalsarraf"),
            )
        except Exception as e:
            logger.warning("Spontaneous art failed: %s", e)

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
        if self._muted_until and now_local() < self._muted_until:
            return
        if self._goodbye_hold_active(now_local()):
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
        fallback_lines = _memory_recall_fallback_lines()

        message = ""
        try:
            from ..fact_extractor import EXTRACTION_MODEL, LM_STUDIO_URL
            from ..llm_json import call_llm_text
            from ..llm_router import get_lm_gate

            gate = get_lm_gate()
            async with gate:
                message = await call_llm_text(
                    LM_STUDIO_URL, EXTRACTION_MODEL,
                    f"You are Klukai, T-Doll squad leader. The Commander is HUMAN (male). "
                    f"You are NOT an AI. A real memory just surfaced and you want to share it "
                    f"warmly — a quiet 'remember when…' message to the Commander. "
                    f"Recall the MOMENT in your own words (2-3 short sentences). Be tender but "
                    f"composed. Do NOT quote the journal entry verbatim — evoke the feeling of it. "
                    f"Affection {self._affection_level}/9.\n"
                    f"Memory to recall (do not quote directly): {annotation[:200]}\n"
                    f"Write ONLY the message. No explanation.",
                    max_tokens=160, temperature=0.8,
                )
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
            logger.info("Memory recall delivered (aff=%d): %s", self._affection_level, message[:60])

    async def _quiet_day_check(self, user_id: str = "jalsarraf") -> None:
        """Pattern-aware check-in for a low-activity day.

        If the activity profiler (``detect_activity_patterns``) finds a strong
        "quiet day" pattern that matches *today's* weekday, and the usual guards
        pass (affection gate, mute, daily cap, last-answered, once/day), Klukai
        gently acknowledges the lull — scaled by closeness. Template-driven so
        it's cheap and deterministic to test; no LLM call.

        Fires at most once per day (``_quiet_day_delivered_today``).
        """
        if self._quiet_day_delivered_today:
            return
        # Needs at least a little warmth before she comments on your silence.
        if self._affection_level < 1:
            return
        # ignore_unanswered: on a quiet day the Commander hasn't replied by
        # definition — an unanswered morning check-in must not veto the one
        # message whose entire purpose is acknowledging that silence.
        if not self._can_send(ignore_unanswered=True):
            return

        try:
            patterns = await self.detect_activity_patterns(user_id)
        except Exception as e:
            logger.debug("Quiet-day pattern lookup failed: %s", e)
            return
        if not patterns:
            return

        today_dow = now_local().weekday()  # Mon=0..Sun=6
        # Translate Python weekday() -> our DOW index (Sun=0..Sat=6) used by
        # the pattern dict, so "today" lines up with the detected pattern.
        today_dow_sunday0 = (today_dow + 1) % 7

        match = None
        for pat in patterns.values():
            if (
                pat.get("type") == "quiet_day"
                and pat.get("dow") == today_dow_sunday0
                and pat.get("confidence", 0.0) >= _QUIET_DAY_CONFIDENCE_FLOOR
            ):
                match = pat
                break
        if not match:
            return

        day_name = match["day"].capitalize()
        message = self._pick_message(QUIET_DAY_MESSAGES).format(day=day_name)

        if self._on_message_callback:
            self._quiet_day_delivered_today = True
            self._proactive_count_today += 1
            self._last_proactive_answered = False
            await self._on_message_callback(message)
            await publish_event("proactive", message)
            logger.info(
                "Quiet-day check-in delivered (day=%s conf=%.2f aff=%d): %s",
                day_name, match.get("confidence", 0.0), self._affection_level,
                message[:60],
            )

    async def _seasonal_check(self) -> None:
        """Deliver a holiday/seasonal greeting when today matches a config event.

        Matches today's (month, day) against ``seasonal_events`` in
        personality.yaml. Fires once per matching occurrence — guarded by a
        per-event delivered key in ``_seasonal_delivered`` (cleared at the daily
        reset), so it sends a single greeting on the day and never repeats it.
        Respects mute + the per-event ``min_affection`` gate. In-character,
        affection-aware templates; no LLM call.
        """
        if self._muted_until and now_local() < self._muted_until:
            return
        if not self._on_message_callback:
            return

        try:
            from ..personality import load_personality
            p = load_personality()
            events = p.get("seasonal_events", {})
        except Exception as e:
            logger.debug("Seasonal config load failed: %s", e)
            return
        if not isinstance(events, dict) or not events:
            return

        now = now_local()
        for key, cfg in events.items():
            if not isinstance(cfg, dict):
                continue
            if cfg.get("month") != now.month or cfg.get("day") != now.day:
                continue
            if self._affection_level < int(cfg.get("min_affection", 0)):
                continue
            # Once per occurrence: skip if already delivered this calendar day.
            guard_key = f"{key}:{now.year}-{now.month:02d}-{now.day:02d}"
            if self._seasonal_delivered.get(guard_key):
                continue
            messages = cfg.get("messages") or []
            if not messages:
                continue

            message = random.choice(messages)
            self._seasonal_delivered[guard_key] = True
            self._proactive_count_today += 1
            self._last_proactive_answered = False
            await self._on_message_callback(message)
            await publish_event("proactive_seasonal", message)
            logger.info("Seasonal greeting delivered (%s): %s", key, message[:60])
            return  # one holiday per day is plenty
