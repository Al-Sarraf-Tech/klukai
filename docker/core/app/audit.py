"""Security audit log writer.

Append-only logging of security-relevant events. Swallows DB errors —
audit logging should never block the primary request path.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .db import get_pool

logger = logging.getLogger(__name__)


# Canonical event types (add to this list when introducing new events)
EVENT_LOGIN_SUCCESS = "login.success"
EVENT_LOGIN_FAILURE = "login.failure"
EVENT_LOGIN_BANNED = "login.banned"
EVENT_LOGOUT = "session.logout"
EVENT_EXPORT_REQUESTED = "export.requested"
EVENT_GIFT_GIVEN = "gift.given"
EVENT_MISSION_STARTED = "mission.started"
EVENT_COSTUME_CHANGED = "costume.changed"
EVENT_PUSH_SUBSCRIBED = "push.subscribed"
EVENT_MEMORY_KEPT = "memory.kept"
EVENT_MEMORY_DISCARDED = "memory.discarded"


async def log(
    event_type: str,
    user_id: str | None = None,
    ip_address: str | None = None,
    request_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append one event to companion_audit_log. Never raises."""
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO companion_audit_log "
                "(event_type, user_id, ip_address, request_id, metadata) "
                "VALUES (%s, %s, %s, %s, %s)",
                (event_type, user_id, ip_address, request_id,
                 json.dumps(metadata) if metadata else None),
            )
    except Exception as e:
        logger.warning("Audit log write failed: event=%s err=%s", event_type, e)


async def recent(
    limit: int = 100,
    event_type: str | None = None,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent audit events (admin viewer). Newest first."""
    limit = max(1, min(limit, 1000))
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            where_parts = []
            params: list[Any] = []
            if event_type:
                where_parts.append("event_type = %s")
                params.append(event_type)
            if user_id:
                where_parts.append("user_id = %s")
                params.append(user_id)
            where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
            params.append(limit)
            rows = await conn.execute(
                "SELECT id, event_type, user_id, ip_address, request_id, "
                "metadata, created_at "
                f"FROM companion_audit_log {where} "
                "ORDER BY created_at DESC LIMIT %s",
                tuple(params),
            )
            return [
                {
                    "id": r[0],
                    "event_type": r[1],
                    "user_id": r[2],
                    "ip_address": r[3],
                    "request_id": r[4],
                    "metadata": r[5],
                    "created_at": r[6].isoformat() if r[6] else None,
                }
                for r in await rows.fetchall()
            ]
    except Exception as e:
        logger.error("Audit log read failed: %s", e)
        return []
