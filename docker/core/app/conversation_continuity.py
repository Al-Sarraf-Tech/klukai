"""Cold-session continuity for one reply.

Redis holds the live thread. When it has only the line he just sent, the
PWA still shows the Postgres history, so the model should see those lines
too. A warm session is left as it is. A database problem degrades to the
live thread and never blocks the reply. Nothing here is deleted.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

CONTINUITY_TURNS = 12
CONTINUITY_CHARS = 480
_ROLES = frozenset({"user", "assistant"})


def _clip(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= CONTINUITY_CHARS:
        return flat
    return flat[: CONTINUITY_CHARS - 1].rstrip() + "…"


def stitch_continuity(
    live: list[dict[str, Any]], earlier: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Persisted lines, then the live session, without repeating his current line.

    ``live`` is oldest-first and already includes the turn being answered.
    When it has any earlier line, Redis is warm and ``earlier`` is ignored.
    """
    if any(str(turn.get("content") or "").strip() for turn in live[:-1]):
        return [
            {
                "role": str(turn.get("role") or ""),
                "content": str(turn.get("content") or ""),
            }
            for turn in live
        ]
    current = live[-1] if live else None
    prefix: list[dict[str, str]] = []
    for turn in earlier:
        role = turn.get("role")
        raw = str(turn.get("content") or "").strip()
        if role not in _ROLES or not raw:
            continue
        prefix.append({"role": role, "content": raw})
    if (
        prefix
        and current is not None
        and prefix[-1]["role"] == current.get("role")
        and prefix[-1]["content"] == str(current.get("content") or "").strip()
    ):
        prefix = prefix[:-1]
    clipped = [
        {"role": turn["role"], "content": _clip(turn["content"])} for turn in prefix
    ]
    return clipped + [
        {"role": str(turn.get("role") or ""), "content": str(turn.get("content") or "")}
        for turn in live
    ]


async def load_recent_thread(user_id: str) -> list[dict[str, str]]:
    """The newest persisted turns, oldest first. Empty when the archive is down."""
    try:
        from .db import get_pool

        pool = get_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT role, content FROM companion_messages "
                "WHERE user_id = %s AND role IN ('user', 'assistant') "
                "AND content IS NOT NULL AND content <> '' "
                "ORDER BY created_at DESC, id DESC LIMIT %s",
                (user_id, CONTINUITY_TURNS),
            )
            rows = await cursor.fetchall()
    except Exception as error:
        logger.warning(
            "Continuity thread unavailable (%s); replying from the live session",
            type(error).__name__,
        )
        return []
    return [{"role": row[0], "content": row[1] or ""} for row in reversed(rows)]
