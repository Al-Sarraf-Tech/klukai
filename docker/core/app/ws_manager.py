"""WebSocket connection manager for companion — multi-device support."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)

SEND_TIMEOUT_SECONDS = 10.0


class WSManager:
    """Manages WebSocket connections per user. Supports multiple devices.

    Outbound only: sends fan out to every device of a user. Inbound frames
    are read by each endpoint coroutine from its own socket (chat.py) — a
    shared cross-socket receive let one device's disconnect tear down another.
    """

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}
        # Per-user background tasks (image gen, reflection, decompression,
        # warmup timers). Cancelled on full-user disconnect so an old session
        # can't write to a stale conn_id or send to a closed WS.
        self._user_tasks: dict[str, set[asyncio.Task]] = {}

    def track_task(self, user_id: str, task: asyncio.Task) -> None:
        """Register a per-user background task. It will be cancelled when the
        user's last device disconnects. Idempotent — auto-removes completed
        tasks via add_done_callback.
        """
        bucket = self._user_tasks.setdefault(user_id, set())
        bucket.add(task)

        def _discard(t: asyncio.Task, _b: set[asyncio.Task] = bucket) -> None:
            _b.discard(t)
            if not t.cancelled():
                error = t.exception()
                if error is not None:
                    logger.error("Background task failed for %s: %s", user_id, error)

        task.add_done_callback(_discard)

    @property
    def connected(self) -> bool:
        return any(conns for conns in self._connections.values())

    def is_connected(self, user_id: str = "default") -> bool:
        return bool(self._connections.get(user_id))

    async def connect(self, ws: WebSocket, user_id: str = "default") -> None:
        await ws.accept()
        if user_id not in self._connections:
            self._connections[user_id] = set()
        self._connections[user_id].add(ws)
        count = len(self._connections[user_id])
        logger.info("WebSocket connected: user=%s (devices=%d)", user_id, count)
        if not await self._send_one(ws, {"type": "connected", "status": "ok"}):
            await self.disconnect(user_id, ws)
            raise ConnectionError("Initial WebSocket send failed")

    async def disconnect(self, user_id: str = "default", ws: WebSocket | None = None) -> None:
        conns = self._connections.get(user_id)
        if conns is not None:
            if ws is None:
                conns.clear()
            else:
                conns.discard(ws)
            if conns:
                return
            self._connections.pop(user_id, None)
        # Detach this generation before awaiting cancellation. A reconnect can
        # create a new bucket while old tasks unwind; never touch that bucket.
        tasks = self._user_tasks.pop(user_id, set())
        current = asyncio.current_task()
        pending = [task for task in tasks if task is not current]
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _send_one(self, ws: WebSocket, data: dict) -> bool:
        """Send to a single WebSocket. Returns False if failed."""
        try:
            await asyncio.wait_for(ws.send_text(json.dumps(data)), SEND_TIMEOUT_SECONDS)
            return True
        except Exception:
            return False

    async def send(self, user_id: str, data: dict) -> None:
        """Broadcast to ALL connected devices for this user."""
        conns = self._connections.get(user_id)
        if not conns:
            return
        for socket in tuple(conns):
            if not await self._send_one(socket, data):
                await self.disconnect(user_id, socket)

    async def send_token(self, user_id: str, text: str) -> None:
        await self.send(user_id, {"type": "token", "text": text})

    async def send_done(self, user_id: str, message_id: str, model: str, final_text: str | None = None) -> None:
        msg = {"type": "done", "message_id": message_id, "model": model}
        if final_text is not None:
            msg["text"] = final_text
        await self.send(user_id, msg)

    async def send_thinking(self, user_id: str, text: str) -> None:
        await self.send(user_id, {"type": "thinking", "text": text})

    async def send_tool_use(self, user_id: str, tool: str, status: str) -> None:
        await self.send(user_id, {"type": "tool_use", "tool": tool, "status": status})

    async def send_mood(self, user_id: str, mood: str) -> None:
        await self.send(user_id, {"type": "mood", "mood": mood})

    async def send_outfit(self, user_id: str, outfit: dict) -> None:
        """She changed (his request was granted) — the PWA updates its chip."""
        await self.send(user_id, {"type": "outfit", "outfit": outfit})

    async def send_proactive(
        self, user_id: str, message: str, *, persist: bool = True
    ) -> None:
        """Deliver a proactive line over WS, optionally persisting it to history.

        Persistence is best-effort and fail-soft: a DB blip must never prevent
        the live push. Stored as role=assistant / model=proactive so Commanders
        still see check-ins, dreams, and level-up lines in history.

        Pass ``persist=False`` for transient UX toasts — "voice link garbled",
        "rendering pipeline broke", and friends. Chat history is SACRED and
        append-only, so an infrastructure hiccup written into it is permanent
        and would also be replayed back to the model as context.
        """
        await self.send(user_id, {"type": "proactive", "message": message})
        if not persist:
            return
        try:
            await self._persist_proactive(user_id, message)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "Proactive persist failed for %s: %s", user_id, e
            )

    async def _persist_proactive(self, user_id: str, message: str) -> None:
        from .db import get_conn
        async with get_conn() as conn:
            row = await (await conn.execute(
                "SELECT id FROM companion_conversations "
                "WHERE user_id = %s ORDER BY started_at DESC LIMIT 1",
                (user_id,),
            )).fetchone()
            if not row:
                # No conversation yet — create a lightweight shell so the
                # message is not lost (SACRED: she spoke; it must land).
                row = await (await conn.execute(
                    "INSERT INTO companion_conversations (user_id, summary, model_used) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (user_id, "proactive", "proactive"),
                )).fetchone()
            conv_id = row[0]
            await conn.execute(
                "INSERT INTO companion_messages "
                "(conversation_id, role, content, model, user_id, content_type) "
                "VALUES (%s, 'assistant', %s, 'proactive', %s, 'proactive')",
                (conv_id, message, user_id),
            )
            # Mirror helpers.store_message so turn_count keeps matching the
            # actual row count — anything reading it would otherwise drift.
            await conn.execute(
                "UPDATE companion_conversations "
                "SET turn_count = turn_count + 1 WHERE id = %s",
                (conv_id,),
            )
            await conn.commit()

    async def send_voice(self, user_id: str, audio_b64: str, final: bool = False) -> None:
        await self.send(user_id, {"type": "voice_audio", "audio": audio_b64, "final": final})

    async def send_affection(self, user_id: str, score: int, level: int, level_name: str, delta: int) -> None:
        await self.send(user_id, {
            "type": "affection", "score": score, "level": level,
            "level_name": level_name, "delta": delta,
        })

    async def send_affection_level_change(self, user_id: str, new_level: int, new_level_name: str, direction: str) -> None:
        await self.send(user_id, {
            "type": "affection_level_change", "level": new_level,
            "level_name": new_level_name, "direction": direction,
        })

    async def send_heartbeat_spike(self, user_id: str, bpm: int, mood: str) -> None:
        """Send a heartbeat spike alert for high-intensity emotional moments."""
        await self.send(user_id, {
            "type": "heartbeat_spike", "bpm": bpm, "mood": mood,
        })
