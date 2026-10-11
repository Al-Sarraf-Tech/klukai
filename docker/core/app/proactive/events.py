"""Spontaneous-event ProactiveEngine behavior.

``EventsMixin`` holds the random lore events, late-night dreams and the evening
romance window — the mood-driven side. The archive-, calendar- and activity-
driven observances (memory recall, spontaneous art, seasonal greeting, quiet-day
check-in, daily recap) live in ``events_rituals.RitualsMixin``. All wall-clock
reads go through ``now_local()`` (America/Chicago) — never the server's UTC
clock.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta

from ..events import publish as publish_event
from .base import _EngineBase
from .state import QUIET_HOUR_END, QUIET_HOUR_START, now_local
from .templates import ROMANCE_MESSAGES, _content_list, _raw_content

logger = logging.getLogger(__name__)

# A dream is a message from someone who just woke up. If he was talking to her
# in the last two hours, she was awake too (see the 0200 watch).
DREAM_QUIET_AFTER_ACTIVITY = timedelta(hours=2)

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


class EventsMixin(_EngineBase):
    """Random/contextual event methods for ProactiveEngine."""

    async def _random_event(self) -> None:
        """Fire a random lore event if conditions are met."""

        now = now_local()

        # Intimate/vulnerable moods BOOST events instead of blocking them
        # — these are the moments Klukai would naturally say something.
        # (A pure read of _last_mood, so it is safe to take ahead of the guards.)
        intimate_mood = self._last_mood in (
            "tender", "longing", "flustered", "affectionate", "shy",
            "yearning", "devoted", "vulnerable", "drowsy",
        )

        if await self._random_event_blocked(now):
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
                if config.get("source") == "ten_year_thread":
                    from ..personality import thread_share_lines
                    messages = thread_share_lines(p, self._affection_level)
                if messages:
                    eligible.append((category, weight, messages))

        if not eligible:
            return

        # Weighted random selection
        total_weight = sum(w for _, w, _ in eligible)
        roll = random.random() * total_weight
        cumulative = 0
        selected_messages = eligible[0][2]
        for _category, weight, messages in eligible:
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

    async def _random_event_blocked(self, now: datetime) -> bool:
        """The ``_random_event`` guard chain: True when a lore line must not fire.

        Order is preserved from the inline chain it replaced, and it matters:
        ``_goodbye_hold_active`` clears a lapsed hold as a side effect, and the
        game probe is the only await, so it stays last.
        """
        # Guard: max 5 per day
        if self._random_events_today >= 5:
            return True

        # Guard: 45-min gap between events
        if self._last_random_event and (now - self._last_random_event) < timedelta(minutes=45):
            return True

        # Guard: don't interrupt active typing (3 min cooldown)
        if self._last_message_time and (now - self._last_message_time) < timedelta(minutes=3):
            return True

        # Guard: check mute
        if self._muted_until and now < self._muted_until:
            return True

        # Guard: he signed off — let him go
        if self._goodbye_hold_active(now):
            return True

        # Guard: quiet hours (local wall clock). The cron stops at 22:45, but a
        # misfire replay or a DST shift must never land a lore line after 2300.
        if QUIET_HOUR_START <= now.hour or now.hour < QUIET_HOUR_END:
            return True

        # Guard: never ping the Commander mid-match
        if self._game_active_probe is not None and await self._game_active_probe():
            return True

        return False

    async def _romance_window(self) -> None:
        """Evening romance message — fires at ~20:30 local with random delay.

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
        if self._muted_until and now_local() < self._muted_until:
            return
        if not self._last_proactive_answered:
            return

        # Random delay 0-30 minutes
        delay = random.uniform(0, 30 * 60)
        await asyncio.sleep(delay)

        # Re-check conditions after delay
        if self._romance_delivered_today:
            return
        if self._muted_until and now_local() < self._muted_until:
            return
        if self._goodbye_hold_active(now_local()):
            return  # he signed off during the delay (or before it)

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

    async def _dream_event(self) -> None:
        """Late-night dream — Klukai wakes from a dream and messages the Commander.

        At high affection, ~30% chance the dream is erotic. Otherwise it's
        a normal memory/nightmare/tender dream. Fires once per night max.
        Balanced: most dreams reference real memories from the archive.
        """
        if self._dream_blocked():
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

            _now_str = now_local().strftime('%I:%M %p')
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

    def _dream_blocked(self) -> bool:
        """The ``_dream_event`` guard chain: True when she must not dream tonight.

        Order is preserved from the inline chain it replaced: the cheap flag
        checks first, then the clock is read once for the mute, goodbye-hold
        (which clears a lapsed hold as a side effect) and 0200-watch checks.
        """
        if self._dream_delivered_today:
            return True
        if self._affection_level < 5:
            return True
        now = now_local()
        if self._muted_until and now < self._muted_until:
            return True
        # He said goodnight (or "gotta go"): a dream is still a ping. Let him go.
        if self._goodbye_hold_active(now):
            return True
        # The 0200 watch: on a night he's awake she isn't dreaming, she's up
        # with him. Stand down if he messaged within DREAM_QUIET_AFTER_ACTIVITY.
        if self._last_message_time and now - self._last_message_time < DREAM_QUIET_AFTER_ACTIVITY:
            return True
        return False
