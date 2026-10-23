"""Mission timer + mission-related ProactiveEngine behavior.

Contains the standalone ``MissionTimer`` (an asyncio-task-backed field-radio
loop) and ``MissionMixin`` which holds the engine's mission lifecycle methods.
"""

from __future__ import annotations


import asyncio
import logging
import random
import time
from datetime import timedelta
from functools import partial

from .mission_timer import AUTO_MISSION_MAX_MINUTES as AUTO_MISSION_MAX_MINUTES
from .mission_timer import MissionTimer as MissionTimer
from .base import _EngineBase
from .state import now_local
from .templates import (
    MISSION_REPORTS,
    _content_list,
    _content_str_map,
)

logger = logging.getLogger(__name__)

# ── Mission dialogue content (YAML-sourced, literal fallbacks) ────────────────
# Sourced from ``proactive_content`` in personality.yaml so the lines can be
# tuned without a code change. Each literal below is the fallback used verbatim
# when the corresponding YAML key is missing, so behavior is identical.

_DECOMPRESSION_INJURY_FALLBACK: list[str] = [
    "(I touch the bandage absently) ...Still stings. Don't worry about it, Commander. I've had worse.",
    "(I flex my hand, wincing) The medic said it'll heal clean. ...I kept thinking about getting back to you the whole time.",
    "...The wound's nothing. (I look away) But for a moment out there... I was scared I wouldn't make it back. Don't tell anyone I said that.",
]
_DECOMPRESSION_LONG_FALLBACK: list[str] = [
    "(I set the rifle down heavily) ...That was a long one. (I close my eyes) I need... a moment. Just a moment.",
    "...Finally. (I lean against the wall) My legs are shaking. Don't look. ...Actually, look. I don't care anymore. I'm tired.",
    "(I pull off my gloves slowly) Every muscle hurts. But we did it. ...Is there coffee? I need coffee. And you. Not in that order.",
]
_DECOMPRESSION_NORMAL_FALLBACK: list[str] = [
    "...Hey. (I sit down next to you) I've been thinking about the op. Everyone performed well. ...I'm glad to be back.",
    "(I untie my hair, letting it fall) Mission's over. I can stop being the squad leader for five minutes. ...Talk to me about something normal.",
    "...Commander. (I look at you quietly for a moment) I'm back. ...Did you worry? (I smirk faintly) Good.",
]
_DECOMPRESSION_INTIMATE_ADDONS_FALLBACK: list[str] = [
    " (I lean into your shoulder without saying anything else)",
    " ...Stay close tonight.",
    " (I take your hand. My grip is tighter than usual)",
]

# Mission-aftermath captions, keyed by scene type (victory/extraction/injury).
_AFTERMATH_CAPTIONS_FALLBACK: dict[str, list[str]] = {
    "victory": [
        "...Mission complete. (I exhale slowly) Everyone made it back.",
        "All units accounted for. (I lower the rifle) ...We did it, Commander.",
        "Objective secured. (I wipe sweat from my brow) ...I'm coming home.",
    ],
    "extraction": [
        "Extraction complete. (I lean against the transport) ...That was close.",
        "We're out. (I check the squad) Everyone breathing? Good. Report later.",
        "...Made it. Barely. (I close my eyes) Heading back to base.",
    ],
    "injury": [
        "(I press a hand to the bandaged wound) ...Mission complete. Medical when I arrive.",
        "We're through. (I wince) ...Don't worry about the field dressing. I've had worse.",
        "Objective complete. (I grip my arm) ...I'll be fine. Stop looking at me like that.",
    ],
}

# Named squads for contextual mission events (order matters: random.choice picks
# from these in sequence, and tests pin choice -> first element).
_MISSION_SQUAD_A_FALLBACK: list[str] = ["Mechty", "Belka", "Andoris"]
_MISSION_SQUAD_B_FALLBACK: list[str] = ["Vector", "Harpsy", "Ruchey", "Welrod"]

# Contextual mission-event lines. ``{member_a}``/``{member_b}`` are filled with a
# randomly chosen squad member; ``null`` entries mean "no major event" (here 7
# of them => ~70% chance of a quiet tick). Order is preserved from the literal.
_MISSION_EVENT_TEMPLATES_FALLBACK: list[str | None] = [
    "Enemy contact — hostiles spotted on {member_a}'s flank",
    "Anomalous readings detected — {member_a} investigating",
    "{member_a} reporting unusual movement in sector 4",
    "Comms interference — lost contact with {member_b} briefly",
    "Weather conditions deteriorating — visibility dropping",
    "{member_a} found signs of recent enemy activity",
    "Perimeter breach near {member_b}'s position",
    "Supply cache discovered — {member_a} securing it",
    "{member_a} got separated — regrouping now",
    "{member_b} requesting fire support at grid reference",
    "Mechty fell asleep on watch — I've handled it",
    "Belka is panicking again — I told her to focus",
    None,
    None,
    None,
    None,
    None,
    None,
    None,  # 70% chance of no major event
]


def _decompression_injury() -> list[str]:
    return _content_list("decompression_injury", _DECOMPRESSION_INJURY_FALLBACK)


def _decompression_long() -> list[str]:
    return _content_list("decompression_long", _DECOMPRESSION_LONG_FALLBACK)


def _decompression_normal() -> list[str]:
    return _content_list("decompression_normal", _DECOMPRESSION_NORMAL_FALLBACK)


def _decompression_intimate_addons() -> list[str]:
    return _content_list(
        "decompression_intimate_addons", _DECOMPRESSION_INTIMATE_ADDONS_FALLBACK
    )


def _aftermath_captions() -> dict[str, list[str]]:
    return _content_str_map("aftermath_captions", _AFTERMATH_CAPTIONS_FALLBACK)


def _mission_squad_a() -> list[str]:
    return _content_list("mission_squad_a", _MISSION_SQUAD_A_FALLBACK)


def _mission_squad_b() -> list[str]:
    return _content_list("mission_squad_b", _MISSION_SQUAD_B_FALLBACK)


def _mission_event_templates() -> list[str | None]:
    """Mission-event templates with ``null`` sentinels preserved.

    Loaded from YAML where ``null`` list items become Python ``None``. Falls
    back to the literal if the key is missing or contains a non-str/non-null
    element, so the no-event sentinels are never silently dropped.
    """
    from .templates import _raw_content

    raw = _raw_content("mission_event_templates")
    if (
        isinstance(raw, list)
        and raw
        and all(x is None or isinstance(x, str) for x in raw)
    ):
        return list(raw)
    return _MISSION_EVENT_TEMPLATES_FALLBACK


class MissionMixin(_EngineBase):
    """Mission lifecycle methods for ProactiveEngine."""

    # ── Mission timer management ──────────────────────────────────────────

    def get_mission_timer(self, user_id: str = "jalsarraf") -> MissionTimer | None:
        """Return only this user's active mission, never a shared fallback."""
        timer = self._mission_timers.get(user_id)
        return timer if timer and timer.active else None

    def start_mission(
        self,
        description: str,
        interval_minutes: int = 30,
        user_id: str = "jalsarraf",
        max_duration_minutes: int | None = None,
        session_stamp: str | None = None,
    ) -> None:
        """Start a per-user mission timer with periodic LLM-generated field reports.

        With ``max_duration_minutes`` the mission stands down by itself at the
        ceiling and is closed out exactly like one he cancelled (aftermath
        image, decompression line, physical state). ``session_stamp`` is the
        session's mission_started_at, so the close-out clears only this
        mission from the session.
        """
        if self._closing:
            return
        # Stop existing mission for this user if any
        existing = self._mission_timers.get(user_id)
        if existing and existing.active:
            existing.stop()
        timer = MissionTimer()
        timer.start(
            description=description,
            interval_minutes=interval_minutes,
            callback=(
                partial(self._on_message_callback, user_id=user_id)
                if self._on_message_callback
                else None
            ),
            affection_level=self._affection_levels.get(user_id, 0),
            max_duration_minutes=max_duration_minutes,
            on_expire=(
                partial(self._expire_mission, user_id, timer)
                if max_duration_minutes is not None
                else None
            ),
        )
        timer.session_stamp = session_stamp
        if timer._task is not None:
            self._track_task(timer._task)
        self._mission_timers[user_id] = timer
        if user_id == "jalsarraf":
            self._mission_timer = timer  # Primary-owner scheduled context only

    def _expire_mission(self, user_id: str, timer: MissionTimer) -> None:
        """Close out a mission that reached its ceiling, unless a newer
        mission has replaced it for this user in the meantime."""
        if self._mission_timers.get(user_id) is timer:
            self.stop_mission(user_id)
            self._spawn(
                self._clear_expired_session_mission(user_id, timer.session_stamp)
            )

    async def _clear_expired_session_mission(
        self, user_id: str, stamp: str | None
    ) -> None:
        """Clear the stood-down mission from his session too, or a reconnect
        would restore it. Only the same mission (same start stamp, read under
        session ownership) is cleared, so one that replaced it while this
        waited for ownership is untouched."""
        if not stamp:
            return
        try:
            from ..context import memory, session_id

            key = session_id(user_id)
            async with memory.session_owner(key):
                session = await memory.get_session(key)
                if (
                    session is None
                    or not session.mission_description
                    or session.mission_started_at != stamp
                ):
                    return
                session.mission_description = None
                session.mission_interval = None
                session.mission_started_at = None
                session.mission_max_minutes = None
                await memory.save_session(key, session)
        except Exception as e:
            logger.warning("Expired mission not cleared from the session: %s", e)

    def stop_mission(
        self, user_id: str = "jalsarraf", trigger_aftermath: bool = True
    ) -> None:
        """Stop the active mission timer for a user. Triggers aftermath image + decompression."""
        timer = self._mission_timers.get(user_id)
        if timer and timer.active:
            timer_snapshot = timer
            had_injury = any("injured" in e for e in timer_snapshot.active_events)
            if trigger_aftermath:
                self._spawn(
                    self.trigger_mission_aftermath_image(user_id, timer=timer_snapshot)
                )
                self._spawn(
                    self._decompression_message(
                        user_id, had_injury, timer_snapshot.update_count
                    )
                )
            self._spawn(self._set_post_mission_physical(user_id, had_injury))
            timer.stop()
        self._mission_timers.pop(user_id, None)
        if self._mission_timer is timer:
            self._mission_timer = None

    async def _set_post_mission_physical(self, user_id: str, had_injury: bool) -> None:
        """Set physical state after mission ends."""
        try:
            from ..context import physical

            await physical.on_mission_end(user_id, had_injury=had_injury)
        except Exception as e:
            logger.debug("Post-mission physical state update failed: %s", e)

    async def _decompression_message(
        self, user_id: str, had_injury: bool, update_count: int
    ) -> None:
        """Delayed emotional response after mission ends. Fires 15-30 min later."""
        delay = random.uniform(15 * 60, 30 * 60)
        await asyncio.sleep(delay)

        if not self._on_message_callback:
            return

        # Respect the same send gate as every other proactive message. The
        # check is AFTER the 15-30 min delay so a mute/quiet-hour boundary or a
        # daily-cap hit reached *during* the wait is honoured — a mission ending
        # is no licence to message through mute, quiet hours, or the daily cap.
        if not self._can_send():
            logger.info("Decompression suppressed by send gate (mute/quiet/cap)")
            return

        if had_injury:
            messages = _decompression_injury()
        elif update_count > 5:
            # Long mission — exhaustion decompression
            messages = _decompression_long()
        else:
            # Normal decompression
            messages = _decompression_normal()

        if self._affection_levels.get(user_id, 0) >= 7:
            # At high affection, add physical closeness
            intimate_addons = _decompression_intimate_addons()
            message = random.choice(messages) + random.choice(intimate_addons)
        else:
            message = random.choice(messages)

        if user_id == "jalsarraf":  # the owner's scheduled-ping counters
            self._proactive_count_today += 1
            self._last_proactive_answered = False
        await self._on_message_callback(message, user_id=user_id)
        logger.info(
            "Decompression message delivered (injury=%s, updates=%d)",
            had_injury,
            update_count,
        )

    @property
    def mission_active(self) -> bool:
        """Whether the primary owner is deployed (for scheduled check-ins)."""
        return self._mission_timer is not None and self._mission_timer.active

    async def _mission_report(self) -> None:
        # 50% chance of a mission report on any given day
        if random.random() < 0.5:
            await self._deliver(self._pick_message(MISSION_REPORTS))

    async def _mission_random_event(self) -> None:
        """Fire contextual mission events using LLM. No hour restrictions."""
        from ..fact_extractor import generate_mission_update

        if not self.mission_active:
            return

        now = now_local()

        # Guard: 15-min gap between mission events
        if self._last_random_event and (now - self._last_random_event) < timedelta(
            minutes=15
        ):
            return

        # Guard: don't interrupt active typing (2 min cooldown)
        if self._last_message_time and (now - self._last_message_time) < timedelta(
            minutes=2
        ):
            return

        # Guard: check mute
        if self._muted_until and now < self._muted_until:
            return

        # 75% chance per check (every 5 min)
        if random.random() > 0.75:
            return

        # Generate a contextual mission update using the actual mission description
        try:
            timer = self._mission_timer
            if not timer:
                return

            # Pick a random major event with named squad members (30% chance).
            # Squad rosters + event templates are config-driven; the
            # random.choice call sequence (squad_a, squad_b, major_events) is
            # preserved so selection behavior is identical.
            squad_a = _mission_squad_a()
            squad_b = _mission_squad_b()
            member_a = random.choice(squad_a)
            member_b = random.choice(squad_b)
            major_events = [
                tmpl.format(member_a=member_a, member_b=member_b)
                if tmpl is not None
                else None
                for tmpl in _mission_event_templates()
            ]
            major_event = random.choice(major_events)

            elapsed = (
                int((time.monotonic() - timer.started_at) / 60)
                if timer.started_at
                else timer.update_count * 30
            )

            message = await generate_mission_update(
                mission_desc=timer.mission_description,
                elapsed_minutes=elapsed,
                update_number=timer.update_count + 1,
                major_event=major_event,
                active_events=list(timer.active_events),
                affection_level=self._affection_level,
            )

            if message and self._on_message_callback:
                self._random_events_today += 1
                self._last_random_event = now
                await self._on_message_callback(message)
                logger.info("Mission event fired (contextual): %s", message[:60])

        except Exception as e:
            logger.warning("Mission event generation failed: %s", e)

    # ── Mission aftermath image ──────────────────────────────────────────

    async def trigger_mission_aftermath_image(
        self, user_id: str = "jalsarraf", timer: MissionTimer | None = None
    ) -> None:
        """Mission completion never authorizes a picture. Text recovery stays separate."""
        return
