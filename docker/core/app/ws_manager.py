"""WebSocket connection manager for companion."""

from __future__ import annotations

import json
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WSManager:
    """Manages the single-user WebSocket connection."""

    def __init__(self) -> None:
        self._ws: WebSocket | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        # Close previous connection if exists (single user)
        if self._ws is not None:
            try:
                await self._ws.close(code=1000, reason="new connection")
            except Exception:
                pass
        self._ws = ws
        logger.info("WebSocket connected")

    async def disconnect(self) -> None:
        self._ws = None
        logger.info("WebSocket disconnected")

    async def send(self, data: dict) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send_text(json.dumps(data))
        except Exception:
            self._ws = None

    async def send_token(self, text: str) -> None:
        await self.send({"type": "token", "text": text})

    async def send_done(self, message_id: str, model: str) -> None:
        await self.send(
            {"type": "done", "message_id": message_id, "model": model}
        )

    async def send_thinking(self, text: str) -> None:
        await self.send({"type": "thinking", "text": text})

    async def send_tool_use(self, tool: str, status: str) -> None:
        await self.send({"type": "tool_use", "tool": tool, "status": status})

    async def send_mood(self, mood: str) -> None:
        await self.send({"type": "mood", "mood": mood})

    async def send_proactive(self, message: str) -> None:
        await self.send({"type": "proactive", "message": message})

    async def send_voice(self, audio_b64: str, final: bool = False) -> None:
        await self.send(
            {"type": "voice_audio", "audio": audio_b64, "final": final}
        )

    async def send_affection(
        self, score: int, level: int, level_name: str, delta: int
    ) -> None:
        await self.send({
            "type": "affection",
            "score": score,
            "level": level,
            "level_name": level_name,
            "delta": delta,
        })

    async def send_affection_level_change(
        self, new_level: int, new_level_name: str, direction: str
    ) -> None:
        await self.send({
            "type": "affection_level_change",
            "level": new_level,
            "level_name": new_level_name,
            "direction": direction,
        })

    async def receive(self) -> dict | None:
        if self._ws is None:
            return None
        try:
            text = await self._ws.receive_text()
            return json.loads(text)
        except Exception:
            self._ws = None
            return None
