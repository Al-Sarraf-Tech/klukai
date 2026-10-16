"""Once-ever scene persistence.

The oath scene and its companion_firsts claim commit together. Live pacing
happens only after that commit and does not write the same lines again.
"""

from __future__ import annotations

import json
import logging

from .db import get_conn
from .proactive.state import now_local

logger = logging.getLogger(__name__)

OATH_EVENT = "oath_fulfilled"


def oath_lines(configured: object) -> list[str] | None:
    """Return the scene only when every entry is a non-empty string.

    An empty or mixed list is invalid and must not consume the trigger.
    """
    if not isinstance(configured, list) or not configured:
        return None
    lines: list[str] = []
    for item in configured:
        if not isinstance(item, str) or not item.strip():
            return None
        lines.append(item)
    return lines


async def commit_oath_scene(user_id: str, lines: list[str]) -> bool:
    """Claim the once-ever trigger and append every line in one transaction.

    Returns True only after commit. A lost race, an existing trigger, or any
    database error leaves both the trigger and the history unchanged.
    """
    scene = oath_lines(lines)
    if scene is None:
        return False
    try:
        async with get_conn() as conn:
            claimed = await conn.execute(
                "INSERT INTO companion_firsts (user_id, event_type, event_date, metadata) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (user_id, event_type) DO NOTHING",
                (
                    user_id,
                    OATH_EVENT,
                    now_local().date(),
                    json.dumps({"lines": len(scene)}),
                ),
            )
            if not claimed.rowcount:
                return False
            row = await (
                await conn.execute(
                    "SELECT id FROM companion_conversations "
                    "WHERE user_id = %s ORDER BY started_at DESC LIMIT 1",
                    (user_id,),
                )
            ).fetchone()
            if row is None:
                row = await (
                    await conn.execute(
                        "INSERT INTO companion_conversations (user_id, summary, model_used) "
                        "VALUES (%s, %s, %s) RETURNING id",
                        (user_id, "proactive", "proactive"),
                    )
                ).fetchone()
            if row is None:
                raise RuntimeError("oath conversation was not created")
            conversation_id = row[0]
            # NOW() is the transaction timestamp, so every line would otherwise
            # share one created_at and a later read could reorder the scene.
            for index, line in enumerate(scene):
                await conn.execute(
                    "INSERT INTO companion_messages "
                    "(conversation_id, role, content, model, user_id, content_type, created_at) "
                    "VALUES (%s, 'assistant', %s, 'proactive', %s, 'proactive', "
                    "NOW() + (%s * INTERVAL '1 millisecond'))",
                    (conversation_id, line, user_id, index),
                )
            await conn.execute(
                "UPDATE companion_conversations "
                "SET turn_count = turn_count + %s WHERE id = %s",
                (len(scene), conversation_id),
            )
            await conn.commit()
            return True
    except Exception as exc:
        logger.warning("Oath scene was not committed for %s: %s", user_id, exc)
        return False
