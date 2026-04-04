"""WebSocket connection manager for companion — multi-user support."""

from __future__ import annotations

import json
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WSManager:
    """Manages WebSocket connections per user."""

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}

    @property
    def connected(self) -> bool:
        return len(self._connections) > 0

    def is_connected(self, user_id: str = "default") -> bool:
        return user_id in self._connections

    async def connect(self, ws: WebSocket, user_id: str = "default") -> None:
        await ws.accept()
        # Close previous connection for this user
        if user_id in self._connections:
            try:
                await self._connections[user_id].close(code=1000, reason="new connection")
            except Exception:
                pass
        self._connections[user_id] = ws
        logger.info("WebSocket connected: user=%s", user_id)
        await self.send(user_id, {"type": "connected", "status": "ok"})

    async def disconnect(self, user_id: str = "default") -> None:
        self._connections.pop(user_id, None)
        logger.info("WebSocket disconnected: user=%s", user_id)

    async def send(self, user_id: str, data: dict) -> None:
        ws = self._connections.get(user_id)
        if ws is None:
            return
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            self._connections.pop(user_id, None)

    async def send_token(self, user_id: str, text: str) -> None:
        await self.send(user_id, {"type": "token", "text": text})

    async def send_done(self, user_id: str, message_id: str, model: str) -> None:
        await self.send(user_id, {"type": "done", "message_id": message_id, "model": model})

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

    async def receive(self, user_id: str = "default") -> dict | None:
        ws = self._connections.get(user_id)
        if ws is None:
            return None
        try:
            text = await ws.receive_text()
            return json.loads(text)
        except Exception:
            self._connections.pop(user_id, None)
            return None
