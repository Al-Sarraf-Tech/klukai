"""WebSocket connection manager for companion — multi-device support."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WSManager:
    """Manages WebSocket connections per user. Supports multiple devices."""

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
        await self._send_one(ws, {"type": "connected", "status": "ok"})

    async def disconnect(self, user_id: str = "default", ws: WebSocket | None = None) -> None:
        conns = self._connections.get(user_id)
        if conns is None:
            return
        if ws is not None:
            conns.discard(ws)
        if not conns:
            self._connections.pop(user_id, None)
            # Last device gone → cancel orphan background tasks for this user
            tasks = self._user_tasks.pop(user_id, set())
            if tasks:
                logger.info("Cancelling %d background tasks for %s on disconnect",
                            len(tasks), user_id)
                for t in tasks:
                    if not t.done():
                        t.cancel()
        logger.info("WebSocket disconnected: user=%s (remaining=%d)", user_id, len(conns) if conns else 0)

    async def _send_one(self, ws: WebSocket, data: dict) -> bool:
        """Send to a single WebSocket. Returns False if failed."""
        try:
            await ws.send_text(json.dumps(data))
            return True
        except Exception:
            return False

    async def send(self, user_id: str, data: dict) -> None:
        """Broadcast to ALL connected devices for this user."""
        conns = self._connections.get(user_id)
        if not conns:
            return
        dead = []
        for ws in conns:
            if not await self._send_one(ws, data):
                dead.append(ws)
        for ws in dead:
            conns.discard(ws)
        if not conns:
            self._connections.pop(user_id, None)

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

    async def send_proactive(self, user_id: str, message: str) -> None:
        await self.send(user_id, {"type": "proactive", "message": message})

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

    async def receive(self, user_id: str = "default") -> dict | None:
        """Receive from ANY connected device for this user using asyncio.wait."""
        import asyncio
        conns = self._connections.get(user_id)
        if not conns:
            return None

        # Create receive tasks for all connected devices — first one to respond wins
        tasks = {}
        for ws in list(conns):
            task = asyncio.create_task(ws.receive_text())
            tasks[task] = ws

        if not tasks:
            return None

        try:
            done, pending = await asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED)

            # Cancel pending tasks
            for task in pending:
                task.cancel()

            # Process the first completed result
            for task in done:
                try:
                    text = task.result()
                    return json.loads(text)
                except json.JSONDecodeError:
                    return None
                except Exception:
                    # This connection died — remove it
                    ws = tasks[task]
                    conns.discard(ws)

        except Exception:
            pass

        if not conns:
            self._connections.pop(user_id, None)
        return None
