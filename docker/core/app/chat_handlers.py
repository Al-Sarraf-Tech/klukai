"""Chat handlers — _maybe_reflect_on_return + _handle_message.

Extracted from app/chat.py for file-size hygiene (S+ Phase 2 §6.1).
Public surface preserved: callers still `from app.chat import _handle_message`
via re-export at the bottom of chat.py.
"""

from __future__ import annotations

import asyncio as asyncio
import logging
import time
from datetime import datetime


from . import context
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
from .image_request_intent import resolve_picture_request
from .memory_archive import update_curation
from .llm_router import FAILURE_SENTINEL
from .models import SessionState, new_id
from .runtime_tasks import complete
from .chat_prompt import ChatPromptBuilder
from .chat_reply import ChatReply, ChatReplyRuntime
from .chat_followups import ChatFollowups
from .personality import (
    assemble_system_prompt,
    load_personality,
)

logger = logging.getLogger(__name__)

# Her Day on the chat path: wait this long for today's row (normally warm from
# connect), and this long for the whole outfit/location step, then go without.
_HER_DAY_WARM_BUDGET_S = 1.5
_HER_DAY_TURN_BUDGET_S = 3.0

# Rollback retains the session owner, so its storage wait must be bounded.
_TURN_ROLLBACK_TIMEOUT_SECONDS = 5.0
# Only interrupted finalization gets a cleanup deadline; healthy storage keeps
# its existing timeout behavior. Match rollback and application shutdown grace.
_TURN_FINALIZATION_TIMEOUT_SECONDS = 5.0

from app.reflect_helpers import (  # noqa: F401,E402
    _maybe_oath_on_connect as _maybe_oath_on_connect,
    _maybe_reflect_on_return as _maybe_reflect_on_return,
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


async def _drop_unanswered_turn(
    sid: str, session: SessionState, *, previous: SessionState | None = None
) -> None:
    """Take the Commander's just-added turn back out of the LIVE session.

    Called when a turn aborts before an answer, including during initial
    persistence, or her core produces no answer. Any user message already
    committed to PostgreSQL stays there — chat history is SACRED and nothing
    is deleted there. This only concerns the 16-turn window replayed to the
    model, not recovery of an uncertain durable write.

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
        if previous is None:
            session.turns.pop()
            session.turn_count = max(0, session.turn_count - 1)
        else:
            # add_turn may have evicted the oldest entry at the context limit.
            # Restore the whole window, not only its tail; keep live metadata.
            session.turns = previous.turns
            session.turn_count = previous.turn_count
        # Hold session ownership until persistence settles, even if shutdown
        # cancels this turn again while its first cancellation is unwinding.
        # Keep the deadline inside the owned task so a stalled Redis response
        # cannot make that cancellation-safe join hold the session forever.
        await complete(
            asyncio.create_task(
                asyncio.wait_for(
                    memory.save_session(sid, session),
                    timeout=_TURN_ROLLBACK_TIMEOUT_SECONDS,
                )
            )
        )


async def _finish_completed_reply(
    sid: str,
    session: SessionState,
    reply: ChatReply,
    user_id: str,
    *,
    interrupted: bool = False,
) -> SessionState:
    """Own one append-only write and live finalization, never terminal delivery."""

    async def persist() -> SessionState:
        # Keep durable history independent of a subsequent Redis failure. Never
        # retry an uncertain append: a lost acknowledgement may still commit.
        await _store_message(
            session.conversation_id,
            "assistant",
            reply.text,
            reply.model,
            latency_ms=reply.latency_ms,
            user_id=user_id,
        )
        return await memory.add_turn(sid, "assistant", reply.text, session)

    task = asyncio.create_task(persist(), name="completed-chat-reply-finalization")

    def interrupted_waiter() -> asyncio.Task[SessionState]:
        return asyncio.create_task(
            asyncio.wait_for(task, timeout=_TURN_FINALIZATION_TIMEOUT_SECONDS)
        )

    waiting = interrupted_waiter() if interrupted else task
    try:
        # Retain the first cancellation and its arguments, including one first
        # received while recovering from an ordinary terminal-delivery error.
        # Only storage is shielded, never provider work or terminal delivery.
        return await asyncio.shield(waiting)
    except asyncio.CancelledError:
        try:
            # Reuse an existing deadline rather than renewing it on cancellation.
            await complete(waiting if interrupted else interrupted_waiter())
        except (Exception, asyncio.CancelledError) as error:
            if not isinstance(error, asyncio.CancelledError):
                logger.warning(
                    "Could not finalize completed reply after cancellation (%s)",
                    type(error).__name__,
                )
        raise


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
            row = await (
                await conn.execute(
                    # make_interval, not a quoted interval literal: psycopg3 does
                    # not bind placeholders inside string literals, and that form
                    # has silently broken queries here before (see
                    # tests/test_sql_bind_guard.py).
                    "SELECT MAX(created_at) FROM companion_messages "
                    "WHERE user_id = %s AND COALESCE(model, '') <> 'proactive' "
                    "AND created_at < NOW() - make_interval(secs => %s)",
                    (user_id, _PRESENCE_IGNORE_SECONDS),
                )
            ).fetchone()
        if not row or not row[0]:
            return None
        last = row[0]
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        gap = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
        return float(max(0.0, gap))
    except Exception as e:
        logger.debug("Presence gap lookup failed: %s", e)
        return None


async def _handle_message(
    content: str, session: SessionState, user_id: str = "default"
) -> str | None:
    """Process a text message: memory recall, LLM, response, extraction.

    Returns her reply text, or None when she had no answer this turn (empty
    input, or the model produced nothing) so callers such as the voice path
    know there is nothing of hers to speak.
    """
    if not content.strip():
        return None

    proactive.mark_user_messaged_today(user_id=user_id)

    start = time.monotonic()
    proactive.mark_responded(user_id=user_id)

    # ── Mission timer detection ───────────────────────────────────────────
    if _wants_mission_cancel(content):
        if proactive.get_mission_timer(user_id) or session.mission_description:
            proactive.stop_mission(user_id=user_id)
            # Clear session mission state
            session.mission_description = None
            session.mission_interval = None
            session.mission_started_at = None
            session.mission_max_minutes = None
            await memory.save_session(session_id(user_id), session)
            logger.info("Mission timer cancelled by user")
        # Don't return — let the message go through to get an in-character response

    elif _wants_mission_start(content):
        interval = _parse_interval_minutes(content)
        # Use last few turns as mission context
        recent = session.turns[-4:] if session.turns else []
        starting_mission_desc = " ".join(
            t.get("content", "")[:100] for t in recent if t.get("role") == "user"
        )
        if not starting_mission_desc:
            starting_mission_desc = content

        aff_state = await affection.get_state(user_id)
        proactive.set_affection_level(aff_state.level, user_id=user_id)
        proactive.start_mission(starting_mission_desc, interval, user_id=user_id)

        # Persist mission state in session so it survives Redis restores
        session.mission_description = starting_mission_desc
        session.mission_interval = interval
        session.mission_started_at = datetime.now().isoformat()
        session.mission_max_minutes = None  # he asked for it: no ceiling
        await memory.save_session(session_id(user_id), session)
        # Record first mission milestone
        await proactive.record_first(user_id, "first_mission")
        logger.info(
            "Mission timer started: every %d min, desc='%s'",
            interval,
            starting_mission_desc[:60],
        )

    completed_reply: ChatReply | None = None

    def capture_completed_reply(reply: ChatReply) -> None:
        nonlocal completed_reply
        # Synchronous: no cancellation point between canonical completion and
        # capturing the immutable reply, before any successful done is visible.
        completed_reply = reply

    # Add user turn to session
    sid = session_id(user_id)
    before_turn = session.model_copy(deep=True)
    try:
        session = await memory.add_turn(sid, "user", content, session)

        # Persist the user message BEFORE streaming — a mid-stream crash can
        # never lose the Commander's message (SACRED). If the store fails, warn
        # the Commander instead of silently swallowing the loss.
        user_stored = await _store_message(
            session.conversation_id, "user", content, user_id=user_id
        )
        if user_stored is False:
            try:
                await ws.send(
                    user_id,
                    {
                        "type": "warning",
                        "message": "...Commander, my memory banks glitched — that message "
                        "may not have been recorded. I'm still listening.",
                    },
                )
            except Exception as e:
                logger.warning("Could not deliver store-failure warning: %s", e)

        # Resolve once from his prior turns, before her reply can add context.
        image_request = resolve_picture_request(
            content, before_turn.turns, detector=needs_image
        )

        def picture_requested(_content: str) -> bool:
            return image_request is not None

        prompt_builder = ChatPromptBuilder(
            memory,
            affection,
            proactive,
            router,
            ws,
            assemble_system_prompt,
            load_personality,
            _wants_thread_inquiry,
            picture_requested,
            _hours_since_last_exchange,
            _HER_DAY_WARM_BUDGET_S,
            _HER_DAY_TURN_BUDGET_S,
        )
        system_prompt = await prompt_builder.build(content, session, user_id)

        # Build messages for LLM — use summary + recent turns for compact context
        messages = []
        if session.context_summary:
            messages.append(
                {
                    "role": "system",
                    "content": f"[Earlier conversation summary: {session.context_summary}]",
                }
            )
        messages.extend(
            {"role": t["role"], "content": t["content"]} for t in session.turns[-16:]
        )

        reply_runtime = ChatReplyRuntime(
            router,
            mcp,
            ws,
            AgentLoop,
            _fix_narration,
            _chunk_text,
            _FAILURE_PREFIX,
            on_completed=capture_completed_reply,
        )
        reply = await reply_runtime.respond(
            content, session, system_prompt, messages, user_id, new_id(), start
        )
    except (Exception, asyncio.CancelledError) as reply_error:
        # A successful done may already have reached another device. Once the
        # canonical answer exists, cancellation must not classify it as absent.
        if completed_reply is not None:
            try:
                await _finish_completed_reply(
                    sid, session, completed_reply, user_id, interrupted=True
                )
            except asyncio.CancelledError:
                # Keep an initiating cancellation (and its arguments), but a
                # new cancellation must supersede an ordinary delivery error.
                if not isinstance(reply_error, asyncio.CancelledError):
                    raise
            except Exception as error:
                logger.warning(
                    "Could not finalize interrupted completed reply (%s)",
                    type(error).__name__,
                )
            raise
        # No complete reply was produced. Keep the already-stored user history,
        # and restore the live window when storage is available.
        try:
            await _drop_unanswered_turn(sid, session, previous=before_turn)
        except Exception as rollback_error:
            # Failed storage cannot repair the snapshot, but must not turn
            # cancellation into a normal return from the admission handler.
            logger.warning(
                "Could not restore live context after interrupted reply (%s)",
                type(rollback_error).__name__,
            )
        raise

    response_text = reply.text

    # Nothing came back from her core: the sentinel (gateway down before any
    # token), a mid-stream drop, or the agent loop's own failure line. The
    # Commander's message is already in PostgreSQL. A non-answer is not
    # recorded as hers, not replayed to the model as context, and not mined
    # for facts, mood or affection — extraction on the sentinel used to reset
    # her mood to "composed", credit a streak and store the outage notice in
    # long-term memory as something she said.
    llm_failed = reply.failed
    if llm_failed:
        logger.info(
            "No answer from the LLM this turn — skipping persistence and follow-ups"
        )
        await _drop_unanswered_turn(sid, session, previous=before_turn)
        return None

    session = await _finish_completed_reply(sid, session, reply, user_id)

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
    await ws.send(
        user_id, {"type": "read_receipt", "read_at": datetime.now().isoformat()}
    )

    followups = ChatFollowups(
        ws,
        context.get_last_memory_id,
        _wants_recall,
        picture_requested,
        do_memory_keep,
        background_recall,
        background_image_gen,
        background_extraction,
        background_compaction,
        SAVE_KEYWORDS,
        DISCARD_KEYWORDS,
        TRIVIAL_PATTERNS,
        COMPACT_THRESHOLD,
        curate=update_curation,
        image_request=image_request,
    )
    await followups.admit(content, response_text, session, user_id)

    return response_text
