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

The pure planning layer (classification, when-hint resolution, slot planning,
``hold_deadline``, ``mentions``) lives in ``op_brief_plan`` and is re-exported
here; this module owns everything that touches the DB, the clock or the rail.
"""

from __future__ import annotations

import json
import logging
import random
import re
import uuid
from dataclasses import dataclass
from typing import Any

from . import deferred
from .db import get_conn
from .op_brief_plan import (
    KIND,
    accept_event,
    hold_deadline,
    mentions,
    plan_slots,
    when_of,
)
from .op_brief_plan import (  # noqa: F401  (re-exported: tests and callers read them here)
    When,
    category,
    classify,
    normalize_what,
    resolve_when,
)
from .proactive.state import LOCAL_TZ, now_local

logger = logging.getLogger(__name__)

ACTION_KIND = "op_brief"
SLOTS = ("brief", "sendoff", "debrief")
VARIANTS = ("normal", "medical", "sensitive")

MAX_EVENTS_PER_TURN = 3
HOLD_STEP_SECONDS = 900

DEFAULT_MIN_AFFECTION = 2
DEFAULT_SLIP_FROM = 5


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
            dup = await (await conn.execute(
                "SELECT id FROM companion_promises "
                "WHERE user_id = %s AND resolved_at IS NULL "
                "AND commitment->>'kind' = %s AND commitment->>'dedupe_key' = %s "
                "LIMIT 1",
                (user_id, KIND, commitment["dedupe_key"]),
            )).fetchone()
            if dup:
                return None
            row = await (await conn.execute(
                "INSERT INTO companion_promises "
                "(user_id, promise_text, commitment, scheduled_followup) "
                "VALUES (%s, %s, %s, NULL) RETURNING id",
                (user_id, commitment["what"], json.dumps(commitment)),
            )).fetchone()
            await conn.commit()
        return str(row[0]) if row else None
    except Exception as e:
        logger.warning("store_event failed for %s: %s", user_id, e)
        return None


async def open_events(user_id: str) -> list[dict[str, Any]]:
    """Unresolved, unfinished event rows (newest first). Fail-soft to []."""
    try:
        async with get_conn() as conn:
            rows = await (await conn.execute(
                "SELECT id, promise_text, commitment FROM companion_promises "
                "WHERE user_id = %s AND resolved_at IS NULL AND followup_sent_at IS NULL "
                "AND commitment->>'kind' = %s ORDER BY made_at DESC LIMIT 20",
                (user_id, KIND),
            )).fetchall()
        return [{"id": str(r[0]), "what": r[1], "commitment": _as_dict(r[2])} for r in rows]
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
        row = await (await conn.execute(
            "SELECT promise_text, commitment, resolved_at, followup_sent_at "
            "FROM companion_promises WHERE id = %s AND user_id = %s",
            (event_id, user_id),
        )).fetchone()
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
    r"\b(cancel\w*|call(?:ed)? off|postpon\w*|reschedul\w*|moved (?:it )?to|pushed back)\b", re.I
)
_NEGATED_CANCEL_RE = re.compile(
    r"\b(?:not|never|no longer)\s+(?:been\s+|be\s+|getting\s+)?(?:cancel|call|postpon|reschedul)"
    r"|n't\s+(?:been\s+|be\s+|getting\s+)?(?:cancel|call|postpon|reschedul)",
    re.I,
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
                    {"kind": ACTION_KIND, "event_id": event_id, "slot": slot,
                     "due": due.isoformat()},
                    user_id=user_id,
                    due_at=due.replace(tzinfo=LOCAL_TZ),
                )
                if task_id is None:
                    logger.warning("Operation brief %s for %s could not be scheduled",
                                   slot, event_id)
            planned += 1
            logger.info("Operation brief planned for %s: %s on %s (%s)",
                        user_id, commitment["what"][:40], commitment["event_date"],
                        ",".join(s for s, _ in slots))
        return planned
    except Exception as e:
        logger.warning("Operation brief planning failed for %s: %s", user_id, e)
        return 0


# ── copy ────────────────────────────────────────────────────────────────────

_DEFAULT_POOLS: dict[str, dict[str, list[str]]] = {
    "normal": {
        "brief": [
            "Briefing for tomorrow.\nSITUATION: {your_what}, tomorrow{when}.\n"
            "MISSION: Go in, get it done.\nEXECUTION: Prepare tonight, not at dawn.\n"
            "SUSTAINMENT: Eat breakfast. That's not a suggestion."
        ],
        "sendoff": ["Today{when}: {the_what}. You're prepared. Go."],
        "debrief": ["Report."],
    },
    "medical": {
        "brief": [
            "Noted for tomorrow{when}: {your_what}. Follow their instructions to the "
            "letter. Theirs, not mine. Leave early."
        ],
        "sendoff": ["Today{when}: {your_what}. Their instructions, exactly. Go."],
        "debrief": ["How did {the_what} go? Keep it short if you like."],
    },
    "sensitive": {
        "brief": ["I know what tomorrow is. No briefing for this one. ...I'm here."],
        "sendoff": ["...I'm here today, Commander. That's all."],
        "debrief": ["...I'm here. No report needed."],
    },
}
_DEFAULT_SLIP = ["...Come back and tell me. That's not a request either."]


@dataclass(frozen=True)
class BriefConfig:
    min_affection: int
    slip_from: int
    slip_lines: list[str]
    pools: dict[str, dict[str, dict[int, list[str]]]]


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _lines(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v.strip()]


def _pool(value: Any) -> dict[int, list[str]]:
    if not isinstance(value, dict):
        return {}
    out: dict[int, list[str]] = {}
    for key, lines in value.items():
        level = _int(key, -1)
        clean = _lines(lines)
        if level >= 0 and clean:
            out[level] = clean
    return out


def load_config() -> BriefConfig:
    """Read ``operation_brief:`` at call time (hot reload), with defaults for
    anything missing or malformed."""
    try:
        from .personality import load_personality
        section = load_personality().get("operation_brief")
    except Exception as e:
        logger.warning("operation_brief config unavailable, using defaults: %s", e)
        section = None
    if not isinstance(section, dict):
        section = {}
    pools: dict[str, dict[str, dict[int, list[str]]]] = {}
    for variant in VARIANTS:
        authored = section.get(variant)
        authored = authored if isinstance(authored, dict) else {}
        pools[variant] = {
            slot: _pool(authored.get(slot)) or {0: _DEFAULT_POOLS[variant][slot]}
            for slot in SLOTS
        }
    return BriefConfig(
        min_affection=_int(section.get("min_affection"), DEFAULT_MIN_AFFECTION),
        slip_from=_int(section.get("slip_from_affection"), DEFAULT_SLIP_FROM),
        slip_lines=_lines(section.get("slip_lines")) or _DEFAULT_SLIP,
        pools=pools,
    )


class _Slots(dict):
    def __missing__(self, key: str) -> str:
        return ""


def _slot_values(commitment: dict[str, Any]) -> _Slots:
    what = str(commitment.get("what") or "it")
    possessive = what.lower().startswith("your ")
    when = ""
    if commitment.get("time_exact") and commitment.get("event_time"):
        when = " at " + str(commitment["event_time"]).replace(":", "")
    return _Slots(
        what=what,
        the_what=what if possessive else f"the {what}",
        your_what=what if possessive else f"your {what}",
        when=when,
    )


def _fill(template: str, values: _Slots, fallback: str) -> str:
    try:
        return template.format_map(values)
    except (ValueError, IndexError, AttributeError):
        return fallback.format_map(values)


def render(slot: str, commitment: dict[str, Any], level: int, cfg: BriefConfig) -> str:
    """Pick and fill the line for ``slot`` at this affection level."""
    variant = commitment.get("variant")
    if variant not in VARIANTS:
        variant = "normal"
    pool = cfg.pools[variant][slot]
    band = max((k for k in pool if k <= level), default=min(pool))
    values = _slot_values(commitment)
    text = _fill(random.choice(pool[band]), values, _DEFAULT_POOLS[variant][slot][0])
    if slot == "brief" and variant != "sensitive" and level >= cfg.slip_from:
        text = f"{text}\n{random.choice(cfg.slip_lines)}"
    text = text.strip()
    return text[:1].upper() + text[1:]


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
    elif proactive._goodbye_hold_active():
        held = "goodbye hold"
    if held:
        if now_local() < hold_deadline(action, commitment):
            await deferred.schedule(dict(action), user_id=user_id,
                                    delay_seconds=HOLD_STEP_SECONDS)
            return
        logger.info("Operation brief %s dropped: %s ran past the hold window", slot, held)
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
        # don't-pile-up gate see it.
        proactive._proactive_count_today += 1
        proactive._last_proactive_answered = False
    except Exception as e:
        logger.warning("Operation brief post-send bookkeeping failed: %s", e)
    await _close(slot, event_id, user_id)
    logger.info("Operation brief %s delivered to %s", slot, user_id)


async def _close(slot: str, event_id: str, user_id: str) -> None:
    if slot == "debrief":
        await mark_complete(event_id, user_id)
