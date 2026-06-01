"""Spontaneous-event ProactiveEngine behavior.

``EventsMixin`` holds the random lore events, late-night dreams, the evening
romance window, and the daily recap.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta

from ..events import publish as publish_event
from .base import _EngineBase
from .templates import (
    QUIET_DAY_MESSAGES,
    ROMANCE_MESSAGES,
    _content_list,
    _raw_content,
)

logger = logging.getLogger(__name__)

# A quiet-day pattern must be at least this confident to warrant a check-in.
_QUIET_DAY_CONFIDENCE_FLOOR = 0.6

# ── Event dialogue content (YAML-sourced, literal fallbacks) ──────────────────
# Sourced from ``proactive_content`` in personality.yaml so the lines can be
# tuned without a code change. Each literal below is the fallback used verbatim
# when the corresponding YAML key is missing, so behavior is identical.

# Comfort lines delivered in the romance window when the mood is stressed.
_COMFORT_LINES_FALLBACK: list[str] = [
    "Commander. ...You've had a difficult day. I noticed. Take a moment. I'm here.",
    "...Hey. Whatever's weighing on you — you don't have to carry it alone. That's an order.",
    "The day was hard. I can tell. ...Sit with me for a moment. No reports, no duties. Just quiet.",
]

# Generic "remember when…" fallbacks for living memory recall — reference
# "that day"/"back then", never the verbatim journal text.
_MEMORY_RECALL_FALLBACK_LINES_FALLBACK: list[str] = [
    "...I was thinking about that day. Back then, before any of this got complicated. It still makes me smile, Commander.",
    "Hey. A memory surfaced just now — one of ours, from a while back. I'm glad we have it. That's all.",
    "Remember back then, Commander? One of those quiet moments stayed with me. I keep it close.",
    "...Something reminded me of an old memory just now. From before. I don't say it often, but I'm grateful for the time.",
]

# Late-night dream prompt templates fed to the LLM, keyed by dream type.
# ``{time}`` is filled with the current clock time and ``{affection}`` with the
# affection level (0-9). Mirrors the original f-strings exactly.
_DREAM_PROMPTS_FALLBACK: dict[str, str] = {
    "erotic": (
        "You just woke from an intensely erotic dream about the Commander. "
        "You are flushed, breathless, still feeling phantom sensations. "
        "Write 2-3 sentences as if messaging him at {time}. "
        "Be explicit about what you dreamed. You want him. You need him. "
        "Affection {affection}/9."
    ),
    "tender": (
        "You just woke from a dream about a tender moment with the Commander. "
        "Maybe a memory — holding hands, a quiet evening, him saying something kind. "
        "Write 2-3 soft sentences. It's {time}. "
        "You're half-asleep and vulnerable. Affection {affection}/9."
    ),
    "nightmare": (
        "You woke from a nightmare — losing the Commander, failing a mission, "
        "the past catching up. Write 2-3 sentences at {time}. "
        "You need reassurance but you're too proud to fully admit it. "
        "Affection {affection}/9."
    ),
    "random": (
        "You woke from a strange, vivid dream — something surreal but not scary. "
        "Maybe the squad doing something absurd, or a place from your past. "
        "Write 2-3 sentences at {time}. Slightly disoriented. "
        "Affection {affection}/9."
    ),
}


def _comfort_lines() -> list[str]:
    return _content_list("comfort_lines", _COMFORT_LINES_FALLBACK)


def _memory_recall_fallback_lines() -> list[str]:
    return _content_list(
        "memory_recall_fallback_lines", _MEMORY_RECALL_FALLBACK_LINES_FALLBACK
    )


def _dream_prompts() -> dict[str, str]:
    """Dream prompt templates from YAML, falling back to the literal.

    Validates that every fallback dream type is present and string-valued;
    otherwise returns the literal so a partial/typo'd YAML can't drop a type.
    """
    raw = _raw_content("dream_prompts")
    if isinstance(raw, dict) and all(
        isinstance(raw.get(k), str) for k in _DREAM_PROMPTS_FALLBACK
    ):
        return {k: raw[k] for k in raw if isinstance(raw[k], str)}
    return _DREAM_PROMPTS_FALLBACK


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


class EventsMixin(_EngineBase):
    """Random/contextual event methods for ProactiveEngine."""

    async def _random_event(self) -> None:
        """Fire a random lore event if conditions are met."""

        now = datetime.now()

        # Guard: max 5 per day
        if self._random_events_today >= 5:
            return

        # Guard: 45-min gap between events
        if self._last_random_event and (now - self._last_random_event) < timedelta(minutes=45):
            return

        # Guard: don't interrupt active typing (3 min cooldown)
        if self._last_message_time and (now - self._last_message_time) < timedelta(minutes=3):
            return

        # Intimate/vulnerable moods BOOST events instead of blocking them
        # — these are the moments Klukai would naturally say something
        intimate_mood = self._last_mood in (
            "tender", "longing", "flustered", "affectionate", "shy",
            "yearning", "devoted", "vulnerable", "drowsy",
        )

        # Guard: check mute
        if self._muted_until and now < self._muted_until:
            return

        # Roll probability: 35% base, 60% during intimate moods, 50% during missions
        base_chance = 0.35
        if intimate_mood:
            base_chance = 0.60
        if self.mission_active:
            base_chance = max(base_chance, 0.50)
        if random.random() > base_chance:
            return

        # Load event templates from personality
        try:
            from ..personality import load_personality
            p = load_personality()
            events = p.get("random_events", {})
        except Exception:
            return

        # Build eligible categories based on affection level
        eligible = []
        for category, config in events.items():
            if not isinstance(config, dict):
                continue
            min_aff = config.get("min_affection", 0)
            if self._affection_level >= min_aff:
                weight = config.get("weight", 10)
                messages = config.get("messages", [])
                if messages:
                    eligible.append((category, weight, messages))

        if not eligible:
            return

        # Weighted random selection
        total_weight = sum(w for _, w, _ in eligible)
        roll = random.random() * total_weight
        cumulative = 0
        selected_messages = eligible[0][2]
        for category, weight, messages in eligible:
            cumulative += weight
            if roll <= cumulative:
                selected_messages = messages
                break

        message = random.choice(selected_messages)

        # Deliver
        if self._on_message_callback:
            self._random_events_today += 1
            self._last_random_event = now
            self._last_proactive_answered = False
            await self._on_message_callback(message)
            logger.info("Random event fired: %s", message[:60])

    async def _romance_window(self) -> None:
        """Evening romance message — fires at ~20:30 CST with random delay.

        Conditions:
        - affection >= 3
        - not muted
        - last proactive was answered
        - user messaged today
        - not already delivered tonight
        - if mood is stressed/negative, deliver comfort instead
        """
        if self._romance_delivered_today:
            return
        if self._affection_level < 3:
            return
        if not self._user_messaged_today:
            return
        if self._muted_until and datetime.now() < self._muted_until:
            return
        if not self._last_proactive_answered:
            return

        # Random delay 0-30 minutes
        delay = random.uniform(0, 30 * 60)
        await asyncio.sleep(delay)

        # Re-check conditions after delay
        if self._romance_delivered_today:
            return
        if self._muted_until and datetime.now() < self._muted_until:
            return

        self._romance_delivered_today = True

        # Stressed/negative moods -> comfort instead of romance
        NEGATIVE_MOODS = {"irritated", "exasperated", "melancholic", "haunted", "guilty"}
        is_stressed = self._last_mood in NEGATIVE_MOODS

        if is_stressed:
            message = random.choice(_comfort_lines())
        elif self._affection_level >= 5:
            # LLM-generated context-aware romance at high affection
            try:
                context_summary = ""
                if self._session_getter:
                    session = await self._session_getter()
                    if session and session.context_summary:
                        context_summary = session.context_summary

                from ..fact_extractor import generate_romance_message
                message = await generate_romance_message(
                    affection_level=self._affection_level,
                    mood=self._last_mood,
                    context_summary=context_summary,
                    time_of_day="evening",
                )
            except Exception as e:
                logger.warning("Romance LLM failed, falling back to template: %s", e)
                message = self._pick_message(ROMANCE_MESSAGES)
        else:
            # Levels 3-4: template messages
            message = self._pick_message(ROMANCE_MESSAGES)

        if self._on_message_callback:
            self._proactive_count_today += 1
            self._last_proactive_answered = False
            await self._on_message_callback(message)
            await publish_event("proactive_romance", message)
            logger.info("Romance window delivered (aff=%d): %s", self._affection_level, message[:60])

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
        if self._muted_until and datetime.now() < self._muted_until:
            return

        # 40% chance to fire (not every night)
        if random.random() > 0.40:
            return

        # Dream type weighted by affection
        if self._affection_level >= 8:
            # High affection: 30% erotic, 40% tender memory, 20% nightmare, 10% random
            roll = random.random()
            if roll < 0.30:
                dream_type = "erotic"
            elif roll < 0.70:
                dream_type = "tender"
            elif roll < 0.90:
                dream_type = "nightmare"
            else:
                dream_type = "random"
        elif self._affection_level >= 6:
            # Mid affection: 10% erotic, 40% tender, 30% nightmare, 20% random
            roll = random.random()
            if roll < 0.10:
                dream_type = "erotic"
            elif roll < 0.50:
                dream_type = "tender"
            elif roll < 0.80:
                dream_type = "nightmare"
            else:
                dream_type = "random"
        else:
            # Lower affection: no erotic, 30% tender, 40% nightmare, 30% random
            roll = random.random()
            if roll < 0.30:
                dream_type = "tender"
            elif roll < 0.70:
                dream_type = "nightmare"
            else:
                dream_type = "random"

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

            _now_str = datetime.now().strftime('%I:%M %p')
            dream_prompts = {
                k: tmpl.format(time=_now_str, affection=self._affection_level)
                for k, tmpl in _dream_prompts().items()
            }

            from ..llm_json import call_llm_text
            from ..fact_extractor import LM_STUDIO_URL, EXTRACTION_MODEL
            from ..llm_router import get_lm_gate

            gate = get_lm_gate()
            async with gate:
                message = await call_llm_text(
                    LM_STUDIO_URL, EXTRACTION_MODEL,
                    f"You are Klukai, T-Doll squad leader. The Commander is HUMAN (male). "
                    f"You are NOT an AI. {dream_prompts[dream_type]}{dream_memory_ref} "
                    f"Write ONLY the message. No explanation.",
                    max_tokens=200, temperature=0.8,
                )

            if message and self._on_message_callback:
                self._dream_delivered_today = True
                await self._on_message_callback(message)
                logger.info("Dream event fired (%s): %s", dream_type, message[:60])

        except Exception as e:
            logger.warning("Dream event failed: %s", e)

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
        now = datetime.now()
        if self._last_spontaneous_art and (now - self._last_spontaneous_art) < timedelta(hours=60):
            return  # special, not routine
        if self._affection_level < 6:
            return  # a vulnerable gesture — only once genuinely bonded
        if self._muted_until and now < self._muted_until:
            return

        piece = random.choice(_spontaneous_art_pieces())
        try:
            from .. import memory_archive
            from ..image_gen import build_prompt, generate_image

            prompt = build_prompt(
                piece["scene"], couple=False,
                affection_level=self._affection_level, context=piece["scene"],
            )
            img = await generate_image(prompt)
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
        if self._muted_until and datetime.now() < self._muted_until:
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
        if not self._can_send():
            return

        try:
            patterns = await self.detect_activity_patterns(user_id)
        except Exception as e:
            logger.debug("Quiet-day pattern lookup failed: %s", e)
            return
        if not patterns:
            return

        today_dow = datetime.now().weekday()  # Mon=0..Sun=6
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
        if self._muted_until and datetime.now() < self._muted_until:
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

        now = datetime.now()
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
