"""The camera button: one tap draws the current situation, no chat turn at all.

There is no typed prompt and no model reply to refuse or ask what to show. The
frame goes straight to the same picture job a typed request uses, scoped to the
signed-in account, and the job reports its own status and any failure.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket

from . import background
from .chat_followups import SCENE_TURN_CHARS, SCENE_TURNS
from .context import memory, session_id, ws
from .memory import SessionUnavailable

logger = logging.getLogger(__name__)

TAP_REQUEST = "take a picture"


async def handle_picture_tap(user_id: str, websocket: WebSocket | None = None) -> bool:
    """Start the current-situation picture. False when the device was retired."""
    key = session_id(user_id)
    async with memory.session_owner(key):
        if websocket is not None and not await ws.validate_connection(
            user_id, websocket, refresh=True
        ):
            return False
        session = await memory.get_session(key)
        if session is None:
            raise SessionUnavailable("Session expired; reconnect and retry")
        turns = [
            {"role": str(t.get("role")), "content": str(t.get("content", ""))}
            for t in list(session.turns)[-SCENE_TURNS:]
            if t.get("role") in {"user", "assistant"}
        ]
    scene_turns = [
        {"role": t["role"], "content": t["content"][:SCENE_TURN_CHARS]} for t in turns
    ]
    chat_ctx = "\n".join(f"{t['role']}: {t['content'][:200]}" for t in turns[-6:])
    ws.track_task(
        user_id,
        asyncio.create_task(
            background.background_image_gen(
                TAP_REQUEST,
                chat_context=chat_ctx,
                squad_members=[],
                user_id=user_id,
                recent_turns=scene_turns,
            )
        ),
        survive_disconnect=True,
    )
    logger.info("Camera tap: current-situation picture started")
    return True
