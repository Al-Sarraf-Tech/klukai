"""Chat prompt assembly — the system prompt and message window for one turn.

Extracted from app/chat_handlers.py for file-size hygiene. This is Phase 2 of
`_handle_message`: memory recall, Her Day (outfit/location), the persistent
personality prompt and the fixed-order per-message blocks. The turn
orchestration, turn recording, reply streaming and follow-ups stay in
chat_handlers, which re-exports these names.

Collaborators are bound at import from app.context — the same way
chat_handlers binds its own — so tests patch `app.chat_prompt.<name>` for the
prompt phase exactly as they patch `app.chat_handlers.<name>` for the rest.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import her_day, rituals, wardrobe
from .context import memory, proactive, router, ws
from .helpers import wants_thread_inquiry as _wants_thread_inquiry
from .image_gen import needs_image
from .models import SessionState
from .personality import (
    assemble_system_prompt,
    build_growth_arc_block,
    build_gaming_block,
    build_inside_jokes_block,
    build_thread_block,
    load_personality,
)

logger = logging.getLogger(__name__)

# Her Day on the chat path: wait this long for today's row (normally warm from
# connect), and this long for the whole outfit/location step, then go without.
_HER_DAY_WARM_BUDGET_S = 1.5
_HER_DAY_TURN_BUDGET_S = 3.0

# The Commander's message for this turn is already persisted by the time the
# system prompt is built, so ignore the last minute when measuring the gap.
_PRESENCE_IGNORE_SECONDS = 60


@dataclass
class _HerDayTurn:
    """What she has on and where she is, as far as this turn could find out."""

    outfit_line: str | None = None
    location_line: str | None = None
    her_where: str | None = None
    request_block: str = ""


async def _recall_context(
    content: str, user_id: str, affection_level: int, is_short: bool,
) -> tuple[list[Any], dict[Any, Any], list[Any]]:
    """Episodes, relationship facts and past exchanges relevant to this message.

    Skipped for very short messages (hi, ok, yes, etc) — expensive and useless.
    """
    if is_short:
        return [], {}, []
    try:
        return await memory.recall_for_prompt(
            content, user_id=user_id, affection_level=affection_level,
        )
    except Exception as e:
        # Defense in depth: recall_for_prompt already fails open, but never let
        # a memory-layer surprise tear down the chat/WS — degrade to no recalled
        # context and keep replying.
        logger.warning("recall_for_prompt failed, continuing with empty context: %s", e)
        return [], {}, []


def _days_together(aff_state: Any) -> int:
    """Calendar days since the first interaction, 0 when unknown."""
    if not aff_state.first_interaction:
        return 0
    try:
        fi = aff_state.first_interaction
        now = datetime.now(fi.tzinfo) if fi.tzinfo else datetime.now()
        return max(0, (now - fi).days)
    except Exception as e:
        logger.debug("Days-together calculation failed: %s", e)
        return 0


def _active_mission_description() -> str | None:
    """Get active mission description for context injection."""
    if proactive.mission_active and hasattr(proactive, '_mission_timer') and proactive._mission_timer:
        return proactive._mission_timer.mission_description
    return None


async def _her_day_for_turn(
    content: str, session: SessionState, user_id: str, aff_state: Any,
    mission_desc: str | None, leaving: bool,
) -> _HerDayTurn:
    """Her Day — what she has on and where she is right now.

    His outfit request, if any, is decided deterministically HERE, before the
    prompt: this reply already knows the outcome, and an image requested in
    the same message renders the change. Bounded and fail-soft: the row is
    normally warm from connect; if not, the hour-based defaults stand this
    turn while the (shielded) pick finishes in the background for the next one.
    """
    turn = _HerDayTurn()

    async def _her_day_turn() -> None:
        warm = asyncio.ensure_future(wardrobe.ensure_today(user_id, aff_state.level, mood=session.mood))
        await asyncio.wait_for(asyncio.shield(warm), _HER_DAY_WARM_BUDGET_S)
        game_active = await router.is_game_active()
        her_now = await her_day.her_now(
            user_id, aff_state.level, mood=session.mood,
            game_active=game_active, mission=mission_desc,
        )
        # A goodbye or the bed order ends the exchange; don't start a fitting.
        requested = None if leaving else wardrobe.detect_outfit_request(content)
        if requested:
            outcome = await wardrobe.handle_request(
                user_id, requested, aff_state.level,
                current_id=her_now.outfit.id, mood=session.mood,
                deployed=bool(mission_desc),
            )
            turn.request_block = wardrobe.request_block(outcome)
            if outcome.decision == "comply":
                her_now = await her_day.her_now(
                    user_id, aff_state.level, mood=session.mood,
                    game_active=game_active, mission=mission_desc,
                )
                await ws.send_outfit(user_id, wardrobe.outfit_payload(
                    her_now.outfit, reason=her_now.outfit_reason,
                    source=her_now.outfit_source, level=aff_state.level,
                ))
        turn.outfit_line = her_now.outfit_line(aff_state.level)
        turn.location_line = her_now.location_line()
        if her_now.activity:
            turn.her_where = f"at the {her_now.location}, {her_now.activity}"
        tea_block = "" if leaving else her_day.tea_time_block(her_now, content, aff_state.level)
        if tea_block:
            turn.request_block = "\n\n".join(b for b in (turn.request_block, tea_block) if b)

    try:
        await asyncio.wait_for(_her_day_turn(), _HER_DAY_TURN_BUDGET_S)
    except Exception as e:  # incl. TimeoutError — she is never kept waiting on her wardrobe
        logger.warning("Her Day unavailable this turn: %s", e)
    return turn


async def _hours_since_last_exchange(user_id: str) -> float | None:
    """Hours since the Commander and Klukai last actually talked.

    Reads real message timestamps — the same source the return-greeting uses —
    so the system prompt and the greeting can't disagree about how long he was
    gone. Her own proactive check-ins are excluded: they would otherwise make
    the gap look shorter than it was. Returns None if the gap can't be
    determined, so the caller can fall back.
    """
    try:
        from datetime import datetime, timezone

        from .db import get_conn

        async with get_conn() as conn:
            row = await (await conn.execute(
                # make_interval, not a quoted interval literal: psycopg3 does
                # not bind placeholders inside string literals, and that form
                # has silently broken queries here before (see
                # tests/test_sql_bind_guard.py).
                "SELECT MAX(created_at) FROM companion_messages "
                "WHERE user_id = %s AND COALESCE(model, '') <> 'proactive' "
                "AND created_at < NOW() - make_interval(secs => %s)",
                (user_id, _PRESENCE_IGNORE_SECONDS),
            )).fetchone()
        if not row or not row[0]:
            return None
        last = row[0]
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        gap = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
        return max(0.0, gap)
    except Exception as e:
        logger.debug("Presence gap lookup failed: %s", e)
        return None


async def _presence_block(aff_state: Any, user_id: str) -> str:
    """Presence / absence coloring (return gap + streak) — fail-soft, "" when none."""
    try:
        from datetime import date as _date, datetime as _dt

        from .personality import build_presence_block
        from .proactive.state import now_local

        # Prefer real message timestamps. `last_interaction_date` is a DATE
        # column, so deriving the gap from it always lands on a multiple of 24 —
        # an overnight 10h absence read as a full day, and disagreed with the
        # return-greeting, which measures the same gap from actual timestamps.
        hours_away = await _hours_since_last_exchange(user_id)
        lid = getattr(aff_state, "last_interaction_date", None)
        if hours_away is None and lid is not None:
            now = now_local()
            if isinstance(lid, _date) and not isinstance(lid, _dt):
                # Date-only fallback: approximate from the calendar day delta.
                hours_away = float((now.date() - lid).days * 24)
            else:
                lid_naive = lid.replace(tzinfo=None) if getattr(lid, "tzinfo", None) else lid
                now_naive = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now
                hours_away = max(0.0, (now_naive - lid_naive).total_seconds() / 3600.0)
        return build_presence_block(
            hours_away=hours_away,
            affection_level=aff_state.level,
            consecutive_days=getattr(aff_state, "consecutive_days", 0) or 0,
        ) or ""
    except Exception as e:
        logger.debug("Presence block skipped: %s", e)
        return ""


def _dream_inquiry_hint(content: str) -> str:
    """Dream inquiry — if Commander asks about dreams, add context."""
    from .helpers import wants_dream_inquiry
    if not wants_dream_inquiry(content):
        return ""
    return (
        "DREAM INQUIRY: The Commander is asking about your dreams. "
        "You DON'T remember your dreams (officially). Deny it with slight "
        "embarrassment: 'Dreams? I don't... dream. I'm a T-Doll.' "
        "But add a subtle tell — a pause, a look away, a faint blush. "
        "The truth is you DID dream about him. You just won't admit it."
    )


def _image_hint(content: str) -> str:
    """Image generation hint — keep text response minimal when an image is coming."""
    if not needs_image(content):
        return ""
    return (
        "IMAGE GENERATION ACTIVE: An image is being generated for this request. "
        "Keep your text response to 1 SHORT sentence — just acknowledge the request. "
        "Do NOT narrate the process of taking a photo, using a data pad, or activating a camera. "
        "The image will appear separately. Example: '...Already done, Commander.' or "
        "'(I smirk) Patience.' Do NOT write more than 1-2 sentences."
    )


def _goodbye_block(content: str, personality: dict, affection_level: int) -> str:
    """Clean goodbyes: he is signing off, so she lets him go (one-shot block),
    and proactive pings hold until morning ("night") or for 90 min ("leave").
    mark_responded() at the top of the turn already lifted any earlier hold."""
    from .goodbyes import build_departure_block
    from .helpers import detect_goodbye
    goodbye = detect_goodbye(content)
    if not goodbye:
        return ""
    if goodbye == "night":
        proactive.mark_goodnight()
    else:
        proactive.mark_leaving()
    return build_departure_block(personality, goodbye, affection_level)


async def _build_system_prompt(
    content: str, session: SessionState, user_id: str, aff_state: Any,
) -> str:
    """Assemble the persistent prompt, then the per-message blocks.

    The per-message blocks (inside jokes, growth arc, presence, ...) are
    appended here — NOT folded into assemble_system_prompt — so the golden
    system-prompt snapshots stay stable. Each block degrades to "" when not
    applicable, and they are appended in a fixed order.
    """
    # Skip expensive memory recall for very short messages (hi, ok, yes, etc)
    is_short = len(content.strip()) <= 20
    episode_memories, rel_facts, recalled_exchanges = await _recall_context(
        content, user_id, aff_state.level, is_short,
    )
    days = _days_together(aff_state)
    mission_desc = _active_mission_description()

    # Detect if Commander is addressing a squad member; jealousy detection —
    # Commander complimenting another T-Doll
    from .helpers import detect_squad_address, detect_jealousy_trigger
    addressed_member = detect_squad_address(content)
    jealousy_target = detect_jealousy_trigger(content)

    # Physical state awareness
    from .context import physical
    phys_state, phys_detail = await physical.get_state(user_id)

    anniversaries = await proactive.check_anniversaries(user_id)
    comfort_objects = await proactive.get_comfort_objects(user_id)

    # Inside jokes / running references (feature: inside jokes). Skipped for
    # very short msgs; surfaced as a per-message block below.
    inside_jokes = [] if is_short else await memory.get_inside_jokes(user_id)

    # Crown jewel tribute — the Commander's most treasured words to Klukai.
    # Only surfaces in the prompt at affection level 4+ (per build_crown_jewel_block).
    from . import tributes
    crown_jewel = await tributes.get_crown_jewel(user_id)

    from .helpers import detect_goodbye as _detect_goodbye
    leaving = bool(_detect_goodbye(content))
    her = await _her_day_for_turn(content, session, user_id, aff_state, mission_desc, leaving)

    system_prompt = assemble_system_prompt(
        mood=session.mood,
        memories=episode_memories,
        relationship_facts=rel_facts,
        recalled_exchanges=recalled_exchanges,
        tools_available=True,
        affection_score=aff_state.score,
        affection_level=aff_state.level,
        days_together=days,
        last_msg_length=len(content),
        mission_description=mission_desc,
        addressed_member=addressed_member,
        jealousy_target=jealousy_target,
        physical_state=phys_state,
        physical_detail=phys_detail,
        anniversaries=anniversaries,
        comfort_objects=comfort_objects,
        crown_jewel=crown_jewel,
        current_outfit=her.outfit_line,
        current_location=her.location_line,
    )

    _p = load_personality()
    blocks: list[str] = [await _presence_block(aff_state, user_id)]

    # Memory nudge — proactive past reference based on affection level
    blocks.append(await memory.get_memory_nudge(session.turn_count, aff_state.level, user_id=user_id) or "")

    # Inside jokes + growth arc. Both degrade to empty strings when not
    # applicable (affection too low, no data, off-cadence).
    _jk_cfg = _p.get("inside_jokes", {})
    if _jk_cfg.get("enabled", True):
        blocks.append(build_inside_jokes_block(
            inside_jokes, aff_state.level,
            max_surfaced=_jk_cfg.get("max_surfaced", 2),
            min_affection_level=_jk_cfg.get("min_affection_level", 3),
        ) or "")
    blocks.append(build_growth_arc_block(_p, aff_state.level, session.turn_count) or "")

    if _wants_thread_inquiry(content):
        blocks.append(build_thread_block(_p, aff_state.level, session.turn_count) or "")

    blocks.append(build_gaming_block(_p, aff_state.level, await router.is_game_active()) or "")

    blocks.append(_goodbye_block(content, _p, aff_state.level))

    # The 0200 watch: 00:30-04:29 local she is already up, quieter and
    # protective, and after a run of late nights she sends him to bed.
    from .night_watch import night_watch_prompt_block
    blocks.append(await night_watch_prompt_block(
        user_id, _p, aff_state.level, memory, activity=her.her_where,
    ) or "")

    # Command decisions: if this answers the call she asked him to make, a
    # one-shot block (effects + the later consequence are applied here); and
    # at 3+, the details of her world his past calls shaped.
    from . import decisions
    blocks.append(await decisions.resolve_prompt_block(user_id, content, memory) or "")
    blocks.append(await decisions.her_world_prompt_block(user_id, aff_state.level, memory) or "")

    blocks.append(await rituals.birthday_prompt_block(
        user_id, _p, aff_state.first_interaction, content,
    ) or "")

    blocks.append(her.request_block)
    blocks.append(_dream_inquiry_hint(content))
    blocks.append(_image_hint(content))

    return system_prompt + "".join(f"\n\n{block}" for block in blocks if block)


def _build_messages(session: SessionState) -> list[dict]:
    """Build messages for LLM — use summary + recent turns for compact context."""
    messages: list[dict] = []
    if session.context_summary:
        messages.append({
            "role": "system",
            "content": f"[Earlier conversation summary: {session.context_summary}]",
        })
    messages.extend(
        {"role": t["role"], "content": t["content"]}
        for t in session.turns[-16:]
    )
    return messages
