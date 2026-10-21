"""Operation Brief — she prepares him for his real-life events.

When the Commander mentions something he has to face ("interview Thursday",
"dentist tomorrow at 3"), the promise extractor's single LLM call also returns
it as an *event*. An accepted event becomes at most one chain of three deferred
one-shots on the ``deferred`` rail (action kind ``op_brief``):

- ``brief``   — T-1 at 20:30 local: a short OPORD in her voice.
- ``sendoff`` — T-0 at 08:05 local: one line. (08:05 rather than 07:55: quiet
  hours end at 08:00 and every slot honours them.)
- ``debrief`` — T-0 at 19:00 local, later if the event runs late: "Report."

Slots already in the past are skipped. Acts of service, not nagging: one chain
per event, and every slot goes through the proactive guard (mute, quiet hours,
daily cap). A slot that lands mid-game, or while a goodbye hold is running, is
held and retried until a deadline.

Variants:

- ``sensitive`` (funeral, hospital, surgery, court, breakup…) — a quiet
  presence line instead of an OPORD. No humour, no "Report." Distress is met
  with protection, never irritation.
- ``medical`` (dentist, doctor, scans…) — briefed, but never told what to eat,
  drink or take. She defers to *their* instructions.
- ``normal`` — everything else.

Storage: rows in ``companion_promises`` with ``commitment.kind = "event"`` and
``scheduled_followup`` NULL, so the promise follow-up path never treats them as
his promises (``promises.due_promises`` also excludes the kind explicitly).
Additive only. Two stamps are ever written after insert: ``followup_sent_at``
when the chain completes, and ``resolved_at`` + ``sentiment='cancelled'`` when
he says it's off.

Copy is authored in ``config/personality.yaml`` (``operation_brief:``) and read
at fire time, so edits hot-reload. Built-in defaults cover a missing or broken
section. Planning is fail-soft; delivery lets *pre-send* failures propagate so
the deferred rail retries them, and swallows everything after the line is out
so a retry can never double-send.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from . import deferred
from .db import get_conn
from .proactive.state import LOCAL_TZ, now_local

from .op_brief_time import (
    BRIEF_AT as BRIEF_AT,
    SENDOFF_AT as SENDOFF_AT,
    DEBRIEF_AT as DEBRIEF_AT,
    NEXT_DAY_DEBRIEF_AT as NEXT_DAY_DEBRIEF_AT,
    LATEST_EVENING as LATEST_EVENING,
    SENDOFF_LEAD as SENDOFF_LEAD,
    DEBRIEF_AFTER as DEBRIEF_AFTER,
    HOLD_STEP_SECONDS as HOLD_STEP_SECONDS,
    HOLD_MAX as HOLD_MAX,
    plan_slots as plan_slots,
    when_of as when_of,
    hold_deadline as hold_deadline,
    WINDOW_DAYS as WINDOW_DAYS,
    _WEEKDAYS as _WEEKDAYS,
    _MONTHS as _MONTHS,
    _NUMBER_WORDS as _NUMBER_WORDS,
    _MONTH_ALT as _MONTH_ALT,
    _WEEKDAY_ALT as _WEEKDAY_ALT,
    _TIME_COLON_RE as _TIME_COLON_RE,
    _TIME_SUFFIX_RE as _TIME_SUFFIX_RE,
    _TIME_MILITARY_RE as _TIME_MILITARY_RE,
    _TIME_AT_RE as _TIME_AT_RE,
    _ISO_RE as _ISO_RE,
    _SLASH_RE as _SLASH_RE,
    _MONTH_DAY_RE as _MONTH_DAY_RE,
    _DAY_MONTH_RE as _DAY_MONTH_RE,
    _ORDINAL_RE as _ORDINAL_RE,
    _IN_DAYS_RE as _IN_DAYS_RE,
    _IN_WEEK_RE as _IN_WEEK_RE,
    _WEEKDAY_RE as _WEEKDAY_RE,
    _PART_OF_DAY as _PART_OF_DAY,
    When as When,
    _hour_from as _hour_from,
    _extract_time as _extract_time,
    _next_month_day as _next_month_day,
    _month_day as _month_day,
    _weekday as _weekday,
    _extract_day as _extract_day,
    resolve_when as resolve_when,
)


from .op_brief_render import (
    SLOTS as SLOTS,
    VARIANTS as VARIANTS,
    DEFAULT_MIN_AFFECTION as DEFAULT_MIN_AFFECTION,
    DEFAULT_SLIP_FROM as DEFAULT_SLIP_FROM,
    _DEFAULT_POOLS as _DEFAULT_POOLS,
    _DEFAULT_SLIP as _DEFAULT_SLIP,
    BriefConfig as BriefConfig,
    _int as _int,
    _lines as _lines,
    _pool as _pool,
    load_config as load_config,
    _Slots as _Slots,
    _slot_values as _slot_values,
    _fill as _fill,
    render as render,
)


from .op_brief_semantics import (
    _SENSITIVE_RE as _SENSITIVE_RE,
    _MEDICAL_RE as _MEDICAL_RE,
    _CATEGORIES as _CATEGORIES,
    _CATEGORY_MAP as _CATEGORY_MAP,
    _STOPWORDS as _STOPWORDS,
    classify as classify,
    category as category,
    normalize_what as normalize_what,
)

logger = logging.getLogger(__name__)

KIND = "event"
ACTION_KIND = "op_brief"

MIN_CONFIDENCE = 0.75
MAX_EVENTS_PER_TURN = 3

# ── classification ──────────────────────────────────────────────────────────


def accept_event(raw: Any, now: datetime) -> dict[str, Any] | None:
    """Validate one extractor event and turn it into a storable commitment."""
    if not isinstance(raw, dict):
        return None
    what = raw.get("what")
    if not isinstance(what, str) or not what.strip():
        return None
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    if confidence < MIN_CONFIDENCE:
        return None
    when = resolve_when(raw.get("when_hint"), now)
    if when is None:
        return None
    clean = normalize_what(what)
    return {
        "kind": KIND,
        "what": clean,
        "when_hint": raw.get("when_hint"),
        "event_date": when.day.isoformat(),
        "event_time": when.at.strftime("%H:%M") if when.at else None,
        "time_exact": when.exact,
        "variant": classify(clean, raw.get("sensitivity")),
        "dedupe_key": f"{when.day.isoformat()}|{category(clean)}",
        "confidence": confidence,
    }


# ── storage (companion_promises, kind=event) ────────────────────────────────


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


async def store_event(commitment: dict[str, Any], user_id: str) -> str | None:
    """Insert one event row unless the same open event already exists.

    Returns the new row id, or None for a duplicate or a DB failure.
    ``scheduled_followup`` stays NULL: event rows are never promise follow-ups.
    """
    try:
        async with get_conn() as conn:
            dup = await (
                await conn.execute(
                    "SELECT id FROM companion_promises "
                    "WHERE user_id = %s AND resolved_at IS NULL "
                    "AND commitment->>'kind' = %s AND commitment->>'dedupe_key' = %s "
                    "LIMIT 1",
                    (user_id, KIND, commitment["dedupe_key"]),
                )
            ).fetchone()
            if dup:
                return None
            row = await (
                await conn.execute(
                    "INSERT INTO companion_promises "
                    "(user_id, promise_text, commitment, scheduled_followup) "
                    "VALUES (%s, %s, %s, NULL) RETURNING id",
                    (user_id, commitment["what"], json.dumps(commitment)),
                )
            ).fetchone()
            await conn.commit()
        return str(row[0]) if row else None
    except Exception as e:
        logger.warning("store_event failed for %s: %s", user_id, e)
        return None


async def open_events(user_id: str) -> list[dict[str, Any]]:
    """Unresolved, unfinished event rows (newest first). Fail-soft to []."""
    try:
        async with get_conn() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, promise_text, commitment FROM companion_promises "
                    "WHERE user_id = %s AND resolved_at IS NULL AND followup_sent_at IS NULL "
                    "AND commitment->>'kind' = %s ORDER BY made_at DESC LIMIT 20",
                    (user_id, KIND),
                )
            ).fetchall()
        return [
            {"id": str(r[0]), "what": r[1], "commitment": _as_dict(r[2])} for r in rows
        ]
    except Exception as e:
        logger.warning("open_events failed for %s: %s", user_id, e)
        return []


async def load_event(event_id: str, user_id: str) -> dict[str, Any] | None:
    """The live event for a firing slot, or None if it is gone, cancelled,
    finished or not an event. DB errors propagate so the rail retries."""
    try:
        uuid.UUID(event_id)
    except ValueError:
        return None
    async with get_conn() as conn:
        row = await (
            await conn.execute(
                "SELECT promise_text, commitment, resolved_at, followup_sent_at "
                "FROM companion_promises WHERE id = %s AND user_id = %s",
                (event_id, user_id),
            )
        ).fetchone()
    if not row or row[2] is not None or row[3] is not None:
        return None
    commitment = _as_dict(row[1])
    if commitment.get("kind") != KIND or not commitment.get("event_date"):
        return None
    return commitment


async def _stamp(sql: str, params: tuple[Any, ...]) -> None:
    try:
        async with get_conn() as conn:
            await conn.execute(sql, params)
            await conn.commit()
    except Exception as e:
        logger.warning("op_brief stamp failed: %s", e)


async def mark_complete(event_id: str, user_id: str) -> None:
    await _stamp(
        "UPDATE companion_promises SET followup_sent_at = NOW() "
        "WHERE id = %s AND user_id = %s AND followup_sent_at IS NULL",
        (event_id, user_id),
    )


async def mark_cancelled(event_id: str, user_id: str, note: str) -> None:
    await _stamp(
        "UPDATE companion_promises SET resolved_at = NOW(), sentiment = 'cancelled', "
        "response_text = %s WHERE id = %s AND user_id = %s AND resolved_at IS NULL",
        (note[:200], event_id, user_id),
    )


# ── cancellation ────────────────────────────────────────────────────────────

_CANCEL_RE = re.compile(
    r"\b(cancel\w*|call(?:ed)? off|postpon\w*|reschedul\w*|moved (?:it )?to|pushed back)\b",
    re.I,
)
_NEGATED_CANCEL_RE = re.compile(
    r"\b(?:not|never|no longer)\s+(?:been\s+|be\s+|getting\s+)?(?:cancel|call|postpon|reschedul)"
    r"|n't\s+(?:been\s+|be\s+|getting\s+)?(?:cancel|call|postpon|reschedul)",
    re.I,
)


def mentions(message: str, commitment: dict[str, Any]) -> bool:
    """Does ``message`` refer to this event (by category or a content word)?"""
    lowered = message.lower()
    pattern = _CATEGORY_MAP.get(category(str(commitment.get("what") or "")))
    if pattern is not None and pattern.search(lowered):
        return True
    words = re.findall(r"[a-z0-9']+", str(commitment.get("what") or "").lower())
    return any(
        len(w) >= 4
        and w not in _STOPWORDS
        and re.search(rf"\b{re.escape(w)}\b", lowered)
        for w in words
    )


async def cancel_mentioned(message: str, user_id: str) -> int:
    """Close any open event he says is cancelled or moved. Regex first, so a
    normal turn never touches the DB. Returns how many were cancelled."""
    if not _CANCEL_RE.search(message) or _NEGATED_CANCEL_RE.search(message):
        return 0
    cancelled = 0
    for event in await open_events(user_id):
        if mentions(message, event["commitment"]):
            await mark_cancelled(event["id"], user_id, message)
            cancelled += 1
    if cancelled:
        logger.info("Operation brief: %d event(s) cancelled for %s", cancelled, user_id)
    return cancelled


# ── planning entry point (background extraction) ────────────────────────────


async def on_turn(
    user_msg: str,
    events: Any,
    *,
    user_id: str,
    affection_level: int,
) -> int:
    """Cancel what he called off, then plan chains for new events.

    Runs in background extraction after the reply. Cancellation goes first so
    "moved to Friday" retires Thursday's chain before Friday's is planned.
    Returns the number of chains scheduled. Never raises.
    """
    try:
        await cancel_mentioned(user_msg, user_id)
        if not isinstance(events, list) or not events:
            return 0
        if affection_level < load_config().min_affection:
            return 0
        now = now_local()
        planned = 0
        for raw in events[:MAX_EVENTS_PER_TURN]:
            commitment = accept_event(raw, now)
            if commitment is None:
                continue
            slots = plan_slots(when_of(commitment), now)
            if not slots:
                continue
            event_id = await store_event(commitment, user_id)
            if not event_id:
                continue
            for slot, due in slots:
                task_id = await deferred.schedule(
                    {
                        "kind": ACTION_KIND,
                        "event_id": event_id,
                        "slot": slot,
                        "due": due.isoformat(),
                    },
                    user_id=user_id,
                    due_at=due.replace(tzinfo=LOCAL_TZ),
                )
                if task_id is None:
                    logger.warning(
                        "Operation brief %s for %s could not be scheduled",
                        slot,
                        event_id,
                    )
            planned += 1
            logger.info(
                "Operation brief planned for %s: %s on %s (%s)",
                user_id,
                commitment["what"][:40],
                commitment["event_date"],
                ",".join(s for s, _ in slots),
            )
        return planned
    except Exception as e:
        logger.warning("Operation brief planning failed for %s: %s", user_id, e)
        return 0


# ── copy ────────────────────────────────────────────────────────────────────

# ── delivery (deferred rail → dispatch → here) ──────────────────────────────


async def _game_active(router: Any) -> bool:
    try:
        return bool(await router.is_game_active())
    except Exception:
        return False


async def deliver(user_id: str, action: dict[str, Any]) -> None:
    """Fire one slot. Held while a game is running or while he has just signed
    off (until its deadline); dropped when the proactive guard says no (muted,
    quiet hours, daily cap) or when affection has fallen below the gate. The
    debrief closes the chain whether it was sent or dropped."""
    slot = str(action.get("slot") or "")
    event_id = str(action.get("event_id") or "")
    if slot not in SLOTS or not event_id or not action.get("due"):
        logger.warning("Malformed op_brief action for %s: %r", user_id, action)
        return
    commitment = await load_event(event_id, user_id)
    if commitment is None:
        return  # cancelled, already finished, or gone

    from .context import affection, proactive, router, ws
    from .decisions import PRIMARY_USER

    cfg = load_config()
    level = (await affection.get_state(user_id)).level
    if level < cfg.min_affection:
        await _close(slot, event_id, user_id)
        return
    # A game, or a goodbye ("gotta go" / goodnight), holds the slot rather than
    # dropping it: the guard below would otherwise refuse the hold and close the
    # slot as delivered, and the OPORD would be lost for good.
    held = None
    if await _game_active(router):
        held = "game"
    elif proactive.goodbye_hold_active_for(user_id):
        held = "goodbye hold"
    if held:
        if now_local() < hold_deadline(action, commitment):
            await deferred.schedule(
                dict(action), user_id=user_id, delay_seconds=HOLD_STEP_SECONDS
            )
            return
        logger.info(
            "Operation brief %s dropped: %s ran past the hold window", slot, held
        )
        await _close(slot, event_id, user_id)
        return
    if not proactive._can_send(ignore_unanswered=True):
        logger.info("Operation brief %s suppressed by the proactive guard", slot)
        await _close(slot, event_id, user_id)
        return

    text = render(slot, commitment, level, cfg)
    await ws.send_proactive(user_id, text)  # persists to history as model=proactive
    try:
        if not ws.is_connected(user_id):
            from .push import send_push

            await send_push(title="Klukai", body=text, user_id=user_id)
        # Count it like any proactive line so the daily cap and the
        # don't-pile-up gate see it. Both belong to the primary owner's
        # scheduled pings, so only his own brief counts there.
        if user_id == PRIMARY_USER:
            proactive._proactive_count_today += 1
            proactive._last_proactive_answered = False
    except Exception as e:
        logger.warning("Operation brief post-send bookkeeping failed: %s", e)
    await _close(slot, event_id, user_id)
    logger.info("Operation brief %s delivered to %s", slot, user_id)


async def _close(slot: str, event_id: str, user_id: str) -> None:
    if slot == "debrief":
        await mark_complete(event_id, user_id)
