"""The Thread — HTTP surface for the app's thread screen.

GET  /api/thread       the entries she is willing to show, with read receipts
POST /api/thread/read  record that the Commander read entries (insert-only)
"""

from __future__ import annotations

import logging
import random

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from . import context
from . import error_codes as ec
from .personality import load_personality
from .personality.thread import unlocked_entries

logger = logging.getLogger(__name__)


class ThreadReadRequest(BaseModel):
    stamps: list[str] = Field(default_factory=list, max_length=64)


async def _get_user_id(request: Request) -> str | None:
    from .auth import get_user_from_token

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return await get_user_from_token(auth[7:])


async def _read_receipts(user_id: str) -> dict[str, str]:
    """stamp -> ISO read time. Fail-soft: a DB blip shows the thread unread."""
    from .db import get_conn

    try:
        async with get_conn() as conn:
            rows = await (await conn.execute(
                "SELECT entry_stamp, read_at FROM companion_thread_reads WHERE user_id = %s",
                (user_id,),
            )).fetchall()
        return {stamp: read_at.isoformat() for stamp, read_at in rows}
    except Exception as e:
        logger.warning("Thread read receipts unavailable: %s", e)
        return {}


def _sealed_line(cfg: dict, level: int) -> str:
    lines = {int(k): v for k, v in cfg.get("sealed_lines", {}).items()}
    reached = [k for k in lines if k <= level]
    return lines[max(reached)] if reached else ""


def register_thread_routes(app: FastAPI) -> None:
    @app.get("/api/thread")
    async def api_thread(request: Request):
        """The ten-year thread, sealed until she is ready to show it."""
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        level = (await context.affection.get_state(user_id)).level
        p = load_personality()
        cfg = p.get("ten_year_thread", {})
        entries = unlocked_entries(p, level)
        if level < cfg.get("share_from_level", 6) or not entries:
            return {"status": "sealed", "message": _sealed_line(cfg, level),
                    "entries": [], "held_back": 0, "reply": None}
        receipts = await _read_receipts(user_id)
        return {
            "status": "open",
            "message": None,
            "entries": [
                {"stamp": e["stamp"], "text": e["text"], "read_at": receipts.get(e["stamp"])}
                for e in entries
            ],
            "held_back": len(cfg.get("entries", [])) - len(entries),
            "reply": {"stamp": cfg.get("last_reply_stamp", ""), "text": cfg.get("last_reply", "I'm here.")},
        }

    @app.post("/api/thread/read")
    async def api_thread_read(req: ThreadReadRequest, request: Request):
        """Mark entries read. Only entries she is currently showing count; the
        first read ever is a relationship first, and she notices."""
        from .db import get_conn_autocommit

        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        level = (await context.affection.get_state(user_id)).level
        p = load_personality()
        visible = {e["stamp"] for e in unlocked_entries(p, level)}
        stamps = [s for s in dict.fromkeys(req.stamps) if s in visible]
        newly_read = 0
        try:
            async with get_conn_autocommit() as conn:
                for stamp in stamps:
                    cur = await conn.execute(
                        "INSERT INTO companion_thread_reads (user_id, entry_stamp) "
                        "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (user_id, stamp),
                    )
                    newly_read += cur.rowcount or 0
        except Exception as e:
            logger.warning("Thread read receipt write failed: %s", e)
            return ec.err(ec.DB_UNAVAILABLE, "Could not record read receipts", status_code=503)

        reactions = p.get("ten_year_thread", {}).get("first_read_reactions", [])
        if newly_read and reactions and await context.proactive.record_first(user_id, "first_thread_read"):
            await context.ws.send_proactive(user_id, random.choice(reactions))
        return {"newly_read": newly_read}
