"""Spontaneous-event ProactiveEngine behavior.

``EventsMixin`` holds the random lore events, late-night dreams, the evening
romance window, and the daily recap. All wall-clock reads go through
``now_local()`` (America/Chicago) — never the server's UTC clock.
"""

from __future__ import annotations
from typing import Any


import asyncio
import logging
import random
from datetime import datetime, timedelta

from ..events import publish as publish_event
from .state import QUIET_HOUR_END, QUIET_HOUR_START, now_local
from .templates import (
    QUIET_DAY_MESSAGES,
    ROMANCE_MESSAGES,
    _content_list,
    _raw_content,
)

from .dream_events import (
    DREAM_QUIET_AFTER_ACTIVITY as DREAM_QUIET_AFTER_ACTIVITY,
    DreamEventsMixin,
)
from .album_events import (
    AlbumEventsMixin,
    _spontaneous_art_pieces as _read_spontaneous_art_pieces,
    _time_of_day as _time_of_day,
    _SPONTANEOUS_ART_PIECES_FALLBACK as _SPONTANEOUS_ART_PIECES_FALLBACK,
)

logger = logging.getLogger(__name__)

# A quiet-day pattern must be at least this confident to warrant a check-in.
_QUIET_DAY_CONFIDENCE_FLOOR = 0.6

# A dream is a message from someone who just woke up. If he was talking to her
# in the last two hours, she was awake too (see the 0200 watch).

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


def _spontaneous_art_pieces() -> list[dict[str, Any]]:
    return _read_spontaneous_art_pieces(_raw_content)


def _select_lore_message(
    events: dict[str, Any], p: dict[str, Any], level: int
) -> str | None:
    # Build eligible categories based on affection level
    eligible = []
    for category, config in events.items():
        if not isinstance(config, dict):
            continue
        min_aff = config.get("min_affection", 0)
        if level >= min_aff:
            weight = config.get("weight", 10)
            messages = config.get("messages", [])
            if config.get("source") == "ten_year_thread":
                from ..personality import thread_share_lines

                messages = thread_share_lines(p, level)
            if messages:
                eligible.append((category, weight, messages))

    if not eligible:
        return None

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

    return str(random.choice(selected_messages))


class EventsMixin(AlbumEventsMixin, DreamEventsMixin):
    """Random/contextual event methods for ProactiveEngine."""

    def _event_now(self) -> datetime:
        return now_local()

    def _event_recall_lines(self) -> list[str]:
        return _memory_recall_fallback_lines()

    def _event_dream_prompts(self) -> dict[str, str]:
        return _dream_prompts()

    def _event_art_pieces(self) -> list[dict[str, Any]]:
        return _spontaneous_art_pieces()

    async def _random_event(self) -> None:
        """Fire a random lore event if conditions are met."""

        now = now_local()

        # Guard: max 5 per day
        if self._random_events_today >= 5:
            return

        # Guard: 45-min gap between events
        if self._last_random_event and (now - self._last_random_event) < timedelta(
            minutes=45
        ):
            return

        # Guard: don't interrupt active typing (3 min cooldown)
        if self._last_message_time and (now - self._last_message_time) < timedelta(
            minutes=3
        ):
            return

        # Intimate/vulnerable moods BOOST events instead of blocking them
        # — these are the moments Klukai would naturally say something
        intimate_mood = self._last_mood in (
            "tender",
            "longing",
            "flustered",
            "affectionate",
            "shy",
            "yearning",
            "devoted",
            "vulnerable",
            "drowsy",
            "cozy",
            "wistful",
            "lonely",
        )

        # Guard: check mute
        if self._muted_until and now < self._muted_until:
            return

        # Guard: he signed off — let him go
        if self._goodbye_hold_active(now):
            return

        # Guard: quiet hours (local wall clock). The cron stops at 22:45, but a
        # misfire replay or a DST shift must never land a lore line after 2300.
        if QUIET_HOUR_START <= now.hour or now.hour < QUIET_HOUR_END:
            return

        # Guard: never ping the Commander mid-match
        if self._game_active_probe is not None and await self._game_active_probe():
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

        message = _select_lore_message(events, p, self._affection_level)
        if message is None:
            return

        # Deliver
        if self._on_message_callback:
            self._random_events_today += 1
            self._last_random_event = now
            self._last_proactive_answered = False
            await self._on_message_callback(message)
            logger.info("Random event fired: %s", message[:60])

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
        NEGATIVE_MOODS = {
            "irritated",
            "exasperated",
            "melancholic",
            "haunted",
            "guilty",
            "heartbroken",
            "overwhelmed",
        }
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
            logger.info(
                "Romance window delivered (aff=%d): %s",
                self._affection_level,
                message[:60],
            )

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
                day_name,
                match.get("confidence", 0.0),
                self._affection_level,
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
