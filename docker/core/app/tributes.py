"""Tribute system — Commander's heartfelt messages to Klukai.

Per feedback_never_delete_chat.md: tributes are SACRED. Once written,
they persist. The only mutation allowed is setting/clearing the
crown-jewel flag (which one tribute is currently "the most treasured").

Per feedback_commander_human.md: Commander is HUMAN — tributes are
written BY the Commander TO Klukai. Klukai never writes tributes
to herself (the LLM cannot generate her own crown jewel).

API surface (used by routes):
- save_tribute(...)        — persist + audit
- count_recent(user_id)    — cooldown check
- get_crown_jewel(user_id) — fetch current pinned tribute (for system prompt)
- list_tributes(...)       — paginated listing for /api/tributes
- set_crown_jewel(...)     — Commander promotes a tribute to crown jewel
"""

from __future__ import annotations

import logging

from .db import get_pool

logger = logging.getLogger(__name__)

# Cooldown between tributes — keeps them rare + meaningful.
TRIBUTE_COOLDOWN_HOURS = 24

# Affection bump per tribute (larger than any single gift; tributes are
# explicitly heartfelt acts).
TRIBUTE_AFFECTION_BUMP = 20

# Min/max text length — enforced at API layer too, this is defense in depth.
MIN_TRIBUTE_LENGTH = 20
MAX_TRIBUTE_LENGTH = 1000


async def count_recent(user_id: str, hours: int = TRIBUTE_COOLDOWN_HOURS) -> int:
    """Count tributes the user has sent in the last `hours`. Used for cooldown."""
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT COUNT(*) FROM companion_tributes "
                "WHERE user_id = %s AND created_at > now() - INTERVAL '%s hours'",
                (user_id, hours),
            )).fetchone()
        return (row or (0,))[0]
    except Exception as e:
        logger.warning("Tribute count failed: %s", e)
        return 0  # Fail-open: a count failure shouldn't block the Commander.


async def save_tribute(
    user_id: str,
    text: str,
    mood_at_time: str | None = None,
    affection_at_time: int | None = None,
    make_crown_jewel: bool = False,
) -> str | None:
    """Insert a new tribute. Returns the new tribute_id (UUID str) or None on error.

    If make_crown_jewel=True, this tribute becomes the new crown jewel
    (any existing crown jewel for the user is demoted first).
    """
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            # Demote existing crown jewel if we're promoting this one
            if make_crown_jewel:
                await conn.execute(
                    "UPDATE companion_tributes SET is_crown_jewel = false "
                    "WHERE user_id = %s AND is_crown_jewel = true",
                    (user_id,),
                )

            row = await (await conn.execute(
                "INSERT INTO companion_tributes "
                "(user_id, text, mood_at_time, affection_at_time, is_crown_jewel) "
                "VALUES (%s, %s, %s, %s, %s) "
                "RETURNING id",
                (user_id, text, mood_at_time, affection_at_time, make_crown_jewel),
            )).fetchone()
            await conn.commit()
        if row:
            tribute_id = str(row[0])
            logger.info(
                "Tribute saved: user=%s id=%s crown=%s len=%d",
                user_id, tribute_id, make_crown_jewel, len(text),
            )
            return tribute_id
    except Exception as e:
        logger.error("Tribute save failed: %s", e)
    return None


async def get_crown_jewel(user_id: str) -> dict | None:
    """Fetch the user's current crown-jewel tribute.

    Returns dict {id, text, created_at, mood_at_time, affection_at_time}
    or None if no crown jewel set.
    """
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT id, text, mood_at_time, affection_at_time, created_at "
                "FROM companion_tributes "
                "WHERE user_id = %s AND is_crown_jewel = true",
                (user_id,),
            )).fetchone()
        if not row:
            return None
        return {
            "id": str(row[0]),
            "text": row[1],
            "mood_at_time": row[2],
            "affection_at_time": row[3],
            "created_at": row[4].isoformat() if row[4] else None,
        }
    except Exception as e:
        logger.warning("Crown jewel fetch failed: %s", e)
        return None


async def list_tributes(user_id: str, limit: int = 20) -> list[dict]:
    """Return newest-first list of the user's tributes."""
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT id, text, mood_at_time, affection_at_time, "
                "is_crown_jewel, created_at "
                "FROM companion_tributes "
                "WHERE user_id = %s "
                "ORDER BY created_at DESC LIMIT %s",
                (user_id, limit),
            )).fetchall()
        return [
            {
                "id": str(r[0]),
                "text": r[1],
                "mood_at_time": r[2],
                "affection_at_time": r[3],
                "is_crown_jewel": r[4],
                "created_at": r[5].isoformat() if r[5] else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("Tribute list failed: %s", e)
        return []


async def set_crown_jewel(user_id: str, tribute_id: str) -> bool:
    """Promote one tribute to crown jewel. Demotes any prior crown jewel."""
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            # Verify the tribute exists + belongs to this user
            row = await (await conn.execute(
                "SELECT id FROM companion_tributes "
                "WHERE id = %s AND user_id = %s",
                (tribute_id, user_id),
            )).fetchone()
            if not row:
                return False

            # Demote any existing crown jewel
            await conn.execute(
                "UPDATE companion_tributes SET is_crown_jewel = false "
                "WHERE user_id = %s AND is_crown_jewel = true",
                (user_id,),
            )

            # Promote the chosen one
            await conn.execute(
                "UPDATE companion_tributes SET is_crown_jewel = true "
                "WHERE id = %s AND user_id = %s",
                (tribute_id, user_id),
            )
            await conn.commit()
        logger.info("Crown jewel set: user=%s id=%s", user_id, tribute_id)
        return True
    except Exception as e:
        logger.error("Crown jewel set failed: %s", e)
        return False


def can_send_tribute(recent_count: int) -> tuple[bool, str | None]:
    """Pure helper: returns (allowed, reason_if_blocked)."""
    if recent_count >= 1:
        return False, f"Tributes are sacred — please wait {TRIBUTE_COOLDOWN_HOURS}h between them"
    return True, None
