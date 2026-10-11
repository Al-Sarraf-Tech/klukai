"""Chat handlers — _maybe_reflect_on_return + _handle_message.

Extracted from app/chat.py for file-size hygiene (S+ Phase 2 §6.1).
Public surface preserved: callers still `from app.chat import _handle_message`
via re-export at the bottom of chat.py.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any


from . import context, her_day, rituals, wardrobe
from .agent_loop import AgentLoop
from .background import (
    background_compaction,
    background_extraction,
    background_image_gen,
    background_recall,
    do_memory_keep,
)
from .context import (
    COMPACT_THRESHOLD,
    affection,
    mcp,
    memory,
    proactive,
    router,
    session_id,
    ws,
)
from .db import get_conn_autocommit
from .helpers import (
    SAVE_KEYWORDS,
    DISCARD_KEYWORDS,
    TRIVIAL_PATTERNS,
    chunk_text as _chunk_text,
    fix_narration as _fix_narration,
    wants_recall as _wants_recall,
    wants_mission_start as _wants_mission_start,
    wants_mission_cancel as _wants_mission_cancel,
    wants_thread_inquiry as _wants_thread_inquiry,
    parse_interval_minutes as _parse_interval_minutes,
    store_message as _store_message,
)
from .image_gen import needs_image
from .llm_router import FAILURE_SENTINEL, LLMStreamFailed
from .models import SessionState, new_id
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

from app.reflect_helpers import (  # noqa: F401,E402
    _maybe_oath_on_connect,
    _maybe_reflect_on_return,
)

REFLECTION_MIN_HOURS_AWAY = 8
REFLECTION_MAX_HOURS_AWAY = 72

# The Commander's message for this turn is already persisted by the time the
# system prompt is built, so ignore the last minute when measuring the gap.
_PRESENCE_IGNORE_SECONDS = 60

# A reply opening like this is not an answer she gave. The router's
# FAILURE_SENTINEL and the agent loop's own failure line ("Communications
# disrupted, Commander. I'll respond with what I have.") share these words,
# so match the prefix rather than the full sentinel.
_FAILURE_PREFIX = FAILURE_SENTINEL.split(",", 1)[0]


async def _drop_unanswered_turn(sid: str, session: SessionState) -> None:
    """Take the Commander's just-added turn back out of the LIVE session.

    Called when her core produced no answer: dominus-nobara is powered off
    (normal), its gateway port is not bound yet after a boot, or the stream
    died. The message itself is already in PostgreSQL — chat history is
    SACRED and nothing is deleted there. This only concerns the 16-turn
    window replayed to the model.

    Design choice: the turn is dropped rather than kept as "context of what
    he said while she was offline". Keeping it would leave consecutive user
    turns with no assistant turn between them — the Mistral-family chat
    template rejects that outright and other models answer the pile as one
    lumped message — and a few "hello?" retries during a power cycle would
    evict real history from the window. Dropping it means that when the GPU
    comes back she picks up exactly where the last real exchange ended, and
    the message he re-sends (the sentinel told him to stand by) is answered
    with clean context. What he said while she was down stays in PostgreSQL
    for the history view and the return greeting.
    """
    if session.turns and session.turns[-1].get("role") == "user":
        session.turns.pop()
        session.turn_count = max(0, session.turn_count - 1)
        await memory.save_session(sid, session)


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


@dataclass
class _HerDayTurn:
    """What she has on and where she is, as far as this turn could find out."""

    outfit_line: str | None = None
    location_line: str | None = None
    her_where: str | None = None
    request_block: str = ""


@dataclass
class _Reply:
    """What came back from her core this turn, already sent to the UI."""

    text: str
    model: str
    latency_ms: int
    stream_failed: bool = False


async def _handle_message(content: str, session: SessionState, user_id: str = "default") -> str | None:
    """Process a text message: memory recall, LLM, response, extraction.

    Returns her reply text, or None when she had no answer this turn (empty
    input, or the local LLM produced nothing) so callers such as the voice
    path know there is nothing of hers to speak.

    The phases run in a fixed order and the order carries invariants: the
    Commander's message is in PostgreSQL before anything streams; a no-answer
    turn returns right after `done` and before anything is recorded as hers;
    follow-ups only run for a real answer.
    """
    if not content.strip():
        return None

    # Preserve relationship activity tracking. This timestamp cannot extend
    # LLM residency; the compatibility keepalive method is a hard no-op.
    from .llm_router import mark_user_active
    mark_user_active()
    proactive.mark_user_messaged_today()

    start = time.monotonic()
    proactive.mark_responded()

    await _handle_mission_commands(content, session, user_id)

    sid = session_id(user_id)
    session = await _record_user_turn(content, session, sid, user_id)

    # Affection is needed both for prompt modulation and for memory re-ranking
    # (importance bias only fires when affection_level is passed through).
    aff_state = await affection.get_state(user_id)
    system_prompt = await _build_system_prompt(content, session, user_id, aff_state)
    messages = _build_messages(session)

    msg_id = new_id()
    if await router.needs_agent(content):
        reply = await _agent_reply(system_prompt, messages, user_id, msg_id, start)
    else:
        reply = await _stream_reply(content, session, system_prompt, messages, user_id, msg_id, start)
        if reply is None:
            # No local backend this turn; the sentinel and `done` are already out.
            await _drop_unanswered_turn(sid, session)
            return None

    # Nothing came back from her core: the sentinel (gateway down before any
    # token), a mid-stream drop, or the agent loop's own failure line. The
    # Commander's message is already in PostgreSQL. A non-answer is not
    # recorded as hers, not replayed to the model as context, and not mined
    # for facts, mood or affection — extraction on the sentinel used to reset
    # her mood to "composed", credit a streak and store the outage notice in
    # long-term memory as something she said.
    if reply.stream_failed or reply.text.startswith(_FAILURE_PREFIX):
        logger.info("No answer from the LLM this turn — skipping persistence and follow-ups")
        await _drop_unanswered_turn(sid, session)
        return None

    session = await _record_assistant_turn(sid, session, user_id, reply)
    _apply_memory_overrides(content, user_id)
    _schedule_followups(content, session, user_id, reply.text)
    return reply.text


# ── Phase 1: the Commander's side of the turn ────────────────────────────────


async def _handle_mission_commands(content: str, session: SessionState, user_id: str) -> None:
    """Mission timer detection: a cancel or a "keep me posted every N min"."""
    if _wants_mission_cancel(content):
        if proactive.mission_active:
            proactive.stop_mission(user_id=user_id)
            # Clear session mission state
            session.mission_description = None
            session.mission_interval = None
            session.mission_started_at = None
            await memory.save_session(session_id(user_id), session)
            logger.info("Mission timer cancelled by user")
        # Don't return — let the message go through to get an in-character response

    elif _wants_mission_start(content):
        interval = _parse_interval_minutes(content)
        # Use last few turns as mission context
        recent = session.turns[-4:] if session.turns else []
        mission_desc = " ".join(t.get("content", "")[:100] for t in recent if t.get("role") == "user")
        if not mission_desc:
            mission_desc = content

        await affection.get_state(user_id)
        proactive.start_mission(mission_desc, interval)

        # Persist mission state in session so it survives Redis restores
        session.mission_description = mission_desc
        session.mission_interval = interval
        session.mission_started_at = datetime.now().isoformat()
        await memory.save_session(session_id(user_id), session)
        # Record first mission milestone
        await proactive.record_first(user_id, "first_mission")
        logger.info("Mission timer started: every %d min, desc='%s'", interval, mission_desc[:60])


async def _record_user_turn(content: str, session: SessionState, sid: str, user_id: str) -> SessionState:
    """Add the user turn to the live session and persist it BEFORE streaming.

    A mid-stream crash can never lose the Commander's message (SACRED). If
    the store fails, warn the Commander instead of silently swallowing the loss.
    """
    session = await memory.add_turn(sid, "user", content, session)

    user_stored = await _store_message(
        session.conversation_id, "user", content, user_id=user_id
    )
    if user_stored is False:
        try:
            await ws.send(user_id, {
                "type": "warning",
                "message": "...Commander, my memory banks glitched — that message "
                           "may not have been recorded. I'm still listening.",
            })
        except Exception as e:
            logger.warning("Could not deliver store-failure warning: %s", e)
    return session


# ── Phase 2: the system prompt ───────────────────────────────────────────────


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


# ── Phase 3: her answer ──────────────────────────────────────────────────────


async def _agent_reply(
    system_prompt: str, messages: list[dict], user_id: str, msg_id: str, start: float,
) -> _Reply:
    """Agentic path: think, use tools, then respond (the agent loop sends its
    own thinking events); the final response is streamed token by token."""
    logger.info("Routing to agent loop (tools needed)")
    agent = AgentLoop(router, mcp, ws)
    agent_result = await agent.run(system_prompt, messages, user_id=user_id)

    response_text = _fix_narration(agent_result.response)
    model_name = agent_result.model

    # Stream the final response token by token for the UI
    for token in _chunk_text(response_text, 8):
        await ws.send_token(user_id, token)
        await asyncio.sleep(0.02)

    latency_ms = int((time.monotonic() - start) * 1000)
    await ws.send_done(user_id, msg_id, model_name)

    logger.info(
        "Agent loop: %d iterations, %d tools used (%s)",
        agent_result.iterations,
        len(agent_result.tools_used),
        ", ".join(agent_result.tools_used) or "none",
    )
    return _Reply(response_text, model_name, latency_ms)


async def _stream_reply(
    content: str, session: SessionState, system_prompt: str, messages: list[dict],
    user_id: str, msg_id: str, start: float,
) -> _Reply | None:
    """Direct path: stream from the local LLM.

    Returns None when there is no local backend this turn — the sentinel and
    `done` have then already been sent, and the caller drops the unanswered
    turn.
    """
    await ws.send_thinking(user_id, "Composing response...")
    try:
        config = await router.route(content, session)
    except RuntimeError as e:
        # No local backend: dominus-nobara is off or its gateway port is
        # not bound yet. That is normal, not a crash — degrade in-band
        # exactly like a pre-token stream failure (the sentinel is the
        # whole reply) instead of letting the WS loop log a traceback and
        # tell him "something glitched" on every turn of the outage.
        logger.warning("Local LLM unavailable this turn: %s", e)
        await ws.send_token(user_id, FAILURE_SENTINEL)
        await ws.send_done(user_id, msg_id, "offline")
        return None
    logger.info("Routing to %s/%s", config.provider, config.model)

    raw_text, stream_failed = await _stream_tokens(system_prompt, messages, config, user_id)

    # Apply narration fix only to the complete text (avoids split-pattern bugs)
    response_text = _fix_narration(raw_text)
    latency_ms = int((time.monotonic() - start) * 1000)

    # Send corrected final text if narration fix changed anything
    final_text = response_text if response_text != raw_text else None
    await ws.send_done(user_id, msg_id, config.model, final_text=final_text)
    return _Reply(response_text, config.model, latency_ms, stream_failed)


async def _stream_tokens(
    system_prompt: str, messages: list[dict], config: Any, user_id: str,
) -> tuple[str, bool]:
    """Relay the token stream to the UI in readable chunks.

    Returns (raw_text, stream_failed). A mid-stream death is reported to the
    UI as an `error` frame and never propagated (it would kill the WS).
    """
    # Fire a delayed "warming up" message if model is slow to respond
    warmup_timer: asyncio.Task | None = None
    if config.provider == "lmstudio":
        async def _warmup_notify():
            await asyncio.sleep(8.0)
            await ws.send_thinking(user_id, "Loading neural pathways, Commander. Stand by...")
        warmup_timer = asyncio.create_task(_warmup_notify())

    full_response = []
    buffer = ""
    first_flush = True
    stream_failed = False
    try:
        async for token in router.stream(system_prompt, messages, config):
            # Cancel warmup message once first token arrives
            if warmup_timer and not warmup_timer.done():
                warmup_timer.cancel()
                warmup_timer = None
            full_response.append(token)
            buffer += token
            # First flush after 20 chars for fast perceived response, then sentence boundaries
            flush_threshold = 20 if first_flush else 80
            if any(c in buffer for c in '.!?\n)') or len(buffer) > flush_threshold:
                await ws.send_token(user_id, buffer)
                buffer = ""
                first_flush = False
    except LLMStreamFailed as e:
        # Mid-stream death: don't propagate (it would kill the WS). The
        # user message is already persisted; surface an explicit error,
        # finalize the truncated reply for the UI, and skip follow-ups.
        stream_failed = True
        logger.error("LLM stream failed mid-response: %s", e)
        await ws.send(user_id, {
            "type": "error",
            "message": "...Connection to my core dropped mid-sentence, "
                       "Commander. Say that again?",
        })
        # The unflushed tail is not sent after the error: a token frame
        # following an error opens a stray second bubble in the client.
        buffer = ""
    if warmup_timer and not warmup_timer.done():
        warmup_timer.cancel()
    if buffer:
        await ws.send_token(user_id, buffer)
    return "".join(full_response), stream_failed


# ── Phase 4: record her answer and schedule the follow-ups ──────────────────


async def _record_assistant_turn(
    sid: str, session: SessionState, user_id: str, reply: _Reply,
) -> SessionState:
    """Add her turn to the live session, mark his messages read, persist hers."""
    session = await memory.add_turn(sid, "assistant", reply.text, session)

    # Mark user messages as read (the user message itself was already
    # persisted before streaming — never stored twice).
    try:
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "UPDATE companion_messages SET read_at = NOW() "
                "WHERE conversation_id = %s AND role = 'user' AND read_at IS NULL "
                "AND user_id = %s",
                (session.conversation_id, user_id),
            )
    except Exception as e:
        logger.warning("Failed to set read_at: %s", e)
    await ws.send(user_id, {"type": "read_receipt", "read_at": datetime.now().isoformat()})

    await _store_message(
        session.conversation_id, "assistant", reply.text, reply.model,
        latency_ms=reply.latency_ms, user_id=user_id,
    )
    return session


def _apply_memory_overrides(content: str, user_id: str) -> None:
    """Commander save/discard overrides — operate on the most recent memory."""
    content_lower = content.lower()
    user_last_mem = context.get_last_memory_id(user_id)
    # Track all post-response background tasks so a full user disconnect cancels
    # them — otherwise a stale task (extraction/compaction) can wake after the
    # user reconnects and clobber the fresh session's write.
    if any(kw in content_lower for kw in SAVE_KEYWORDS) and user_last_mem:
        ws.track_task(user_id, asyncio.create_task(do_memory_keep(user_last_mem, kept=True)))
    elif any(kw in content_lower for kw in DISCARD_KEYWORDS) and user_last_mem:
        ws.track_task(user_id, asyncio.create_task(do_memory_keep(user_last_mem, kept=False)))


def _schedule_followups(content: str, session: SessionState, user_id: str, response_text: str) -> None:
    """Background work for a turn the LLM answered: recall or image generation,
    fact extraction, compaction. Every task is tracked for cancellation."""
    # Track whether image gen was triggered (for extraction curation pass)
    image_triggered = False

    # Recall detection takes priority over new image generation.
    if _wants_recall(content):
        logger.info("Memory recall triggered for: %s", content[:80])
        ws.track_task(user_id, asyncio.create_task(background_recall(content, session, user_id)))
    elif needs_image(content):
        logger.info("Image generation triggered for: %s", content[:80])
        # Build chat context from last few turns for scene-aware prompting
        recent_turns = session.turns[-6:]  # Last 3 exchanges
        chat_ctx = "\n".join(
            f"{t['role']}: {t['content'][:200]}" for t in recent_turns
        )
        image_triggered = True
        # Detect squad members mentioned for multi-character scenes
        from .image_gen import detect_squad_members
        mentioned_squad = detect_squad_members(f"{content} {chat_ctx}")
        ws.track_task(user_id, asyncio.create_task(background_image_gen(
            content, chat_context=chat_ctx, squad_members=mentioned_squad,
            user_id=user_id,
        )))

    # Background: extract facts and create episodes
    # Skip extraction for trivial messages (no facts to extract, saves a full LLM round-trip)
    content_stripped = content.strip().lower().rstrip("!.?)")
    is_trivial = content_stripped in TRIVIAL_PATTERNS or (
        len(content_stripped) <= 5 and not image_triggered
    )

    if not is_trivial:
        async def _safe_extraction():
            try:
                await background_extraction(
                    content, response_text, session, user_id,
                    image_generated=image_triggered,
                )
            except Exception as e:
                import traceback
                logger.error("EXTRACTION CRASH: %s\n%s", e, traceback.format_exc())
        ws.track_task(user_id, asyncio.create_task(_safe_extraction()))
    else:
        logger.info("Trivial message, skipping extraction: %s", content[:40])

    # Background: compact session if turns exceed threshold
    if len(session.turns) >= COMPACT_THRESHOLD:
        ws.track_task(user_id, asyncio.create_task(background_compaction(session, user_id)))
