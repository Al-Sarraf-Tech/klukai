"""Promises & gentle accountability.

When the Commander commits to something ("I'll fix it tomorrow", "I'm going
to call her"), Klukai quietly remembers and schedules a caring follow-up. A
proactive job (wired into engine.py by the orchestrator) polls due_promises()
on its scheduler tick, delivers followup_message(), and calls
mark_followup_sent(). The /api/promises/{id}/resolve endpoint closes the loop
with the Commander's reply + a sentiment.

DB access mirrors app/memory.py / app/dreams.py: psycopg with parameterized
queries, and every public helper fails soft (None / [] / False) so a DB hiccup
can never crash the background extraction or the scheduler tick.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from .db import get_conn

logger = logging.getLogger(__name__)

# Sane default when the Commander gives no deadline hint: nudge ~20h later, so
# a thing promised "tonight" or "tomorrow" gets a check-in the next day without
# nagging within the same evening.
DEFAULT_FOLLOWUP_HOURS = 20


async def store_promise(
    commitment: dict,
    user_id: str = "jalsarraf",
    scheduled_followup: datetime | None = None,
) -> str | None:
    """Persist a detected commitment and return its UUID (or None on failure).

    ``commitment`` is the per-promise dict from extract_promises(), e.g.
    ``{"action": "fix the door", "target": "...", "deadline_hint": "tomorrow",
    "confidence": 0.9}``. If ``scheduled_followup`` is omitted we compute a
    sane default (+DEFAULT_FOLLOWUP_HOURS). An empty/whitespace action is a
    no-op (returns None without touching the DB) — there's nothing to follow
    up on.
    """
    action = (commitment.get("action") or "").strip() if isinstance(commitment, dict) else ""
    if not action:
        return None

    if scheduled_followup is None:
        scheduled_followup = datetime.now(timezone.utc) + timedelta(
            hours=DEFAULT_FOLLOWUP_HOURS
        )

    try:
        async with get_conn() as conn:
            row = await (await conn.execute(
                "INSERT INTO companion_promises "
                "(user_id, promise_text, commitment, scheduled_followup) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (user_id, action, json.dumps(commitment), scheduled_followup),
            )).fetchone()
            await conn.commit()
        if not row:
            return None
        promise_id = str(row[0])
        logger.info("Promise stored for %s: %s (followup %s)",
                    user_id, action[:60], scheduled_followup.isoformat())
        return promise_id
    except Exception as e:
        logger.warning("store_promise failed for %s: %s", user_id, e)
        return None


async def due_promises(user_id: str, now: datetime) -> list[dict]:
    """Return unresolved promises whose follow-up time has arrived, oldest first.

    Called from the proactive scheduler tick. Fails soft to [] so a DB outage
    just skips this round of follow-ups rather than crashing the engine loop.
    """
    try:
        async with get_conn() as conn:
            rows = await (await conn.execute(
                "SELECT id, promise_text, commitment, made_at, scheduled_followup "
                "FROM companion_promises "
                "WHERE user_id = %s AND scheduled_followup <= %s "
                "AND resolved_at IS NULL AND followup_sent_at IS NULL "
                "ORDER BY scheduled_followup ASC",
                (user_id, now),
            )).fetchall()
        out: list[dict] = []
        for r in rows:
            commitment = r[2]
            # psycopg returns JSONB as a dict already; tolerate a str just in case.
            if isinstance(commitment, str):
                try:
                    commitment = json.loads(commitment)
                except Exception:
                    commitment = {}
            out.append({
                "id": str(r[0]),
                "promise_text": r[1],
                "commitment": commitment or {},
                "made_at": r[3].isoformat() if r[3] else None,
                "scheduled_followup": r[4].isoformat() if r[4] else None,
            })
        return out
    except Exception as e:
        logger.warning("due_promises failed for %s: %s", user_id, e)
        return []


async def open_promises(user_id: str, limit: int = 50) -> list[dict]:
    """All unresolved promises for a user, newest first (for the REST view).

    Fails soft to [] so the endpoint degrades to "no open promises" rather than
    500-ing if the store is briefly unavailable.
    """
    try:
        async with get_conn() as conn:
            rows = await (await conn.execute(
                "SELECT id, promise_text, commitment, made_at, scheduled_followup, "
                "followup_sent_at FROM companion_promises "
                "WHERE user_id = %s AND resolved_at IS NULL "
                "ORDER BY made_at DESC LIMIT %s",
                (user_id, limit),
            )).fetchall()
        out: list[dict] = []
        for r in rows:
            commitment = r[2]
            if isinstance(commitment, str):
                try:
                    commitment = json.loads(commitment)
                except Exception:
                    commitment = {}
            out.append({
                "id": str(r[0]),
                "promise_text": r[1],
                "commitment": commitment or {},
                "made_at": r[3].isoformat() if r[3] else None,
                "scheduled_followup": r[4].isoformat() if r[4] else None,
                "followup_sent": r[5] is not None,
            })
        return out
    except Exception as e:
        logger.warning("open_promises failed for %s: %s", user_id, e)
        return []


async def mark_followup_sent(promise_id: str) -> bool:
    """Stamp followup_sent_at=NOW() after the scheduler delivers the nudge."""
    try:
        async with get_conn() as conn:
            await conn.execute(
                "UPDATE companion_promises SET followup_sent_at = NOW() "
                "WHERE id = %s",
                (promise_id,),
            )
            await conn.commit()
        return True
    except Exception as e:
        logger.warning("mark_followup_sent failed for %s: %s", promise_id, e)
        return False


async def resolve_promise(
    promise_id: str, sentiment: str, response_text: str | None = None,
    user_id: str = "jalsarraf",
) -> bool:
    """Close a promise with the Commander's response + a sentiment.

    Scoped to the owning user_id so one authenticated user can't resolve
    another's promise (IDOR). Only touches still-open rows (resolved_at IS NULL)
    so a double-resolve is a safe no-op and can't clobber the original timestamp.
    Returns True only if a row was actually updated.
    """
    try:
        async with get_conn() as conn:
            cur = await conn.execute(
                "UPDATE companion_promises "
                "SET resolved_at = NOW(), sentiment = %s, response_text = %s "
                "WHERE id = %s AND user_id = %s AND resolved_at IS NULL",
                (sentiment, response_text, promise_id, user_id),
            )
            await conn.commit()
            return getattr(cur, "rowcount", 0) > 0
    except Exception as e:
        logger.warning("resolve_promise failed for %s: %s", promise_id, e)
        return False


def followup_message(promise: dict, affection_level: int) -> str:
    """Render an in-character follow-up nudge, tone scaled by affection.

    Low affection (0-2): light, almost businesslike — a brief check-in.
    Mid affection (3-6): warmer, attentive.
    High affection (7-9): openly caring, gentle, no pressure.

    Never raises and never emits a bare "None": falls back to a generic
    "something you mentioned" when neither the commitment action nor the stored
    promise_text is available.
    """
    commitment = promise.get("commitment") or {}
    action = ""
    if isinstance(commitment, dict):
        action = (commitment.get("action") or "").strip()
    if not action:
        action = (promise.get("promise_text") or "").strip()
    if not action:
        action = "something you mentioned"

    if affection_level <= 2:
        return (
            f"Commander — a quick check. You said you'd {action}. "
            f"Did that get handled?"
        )
    if affection_level <= 6:
        return (
            f"Commander, I remembered: you were going to {action}. "
            f"How did it go? I've been keeping an eye out."
        )
    return (
        f"Hey, Commander... I haven't forgotten you wanted to {action}. "
        f"No pressure — I just care how it turned out. Tell me when you can."
    )
