"""Authenticated voice transcription (Fish Audio) and its fail-soft wire responses."""

from __future__ import annotations
from collections.abc import Awaitable, Callable
import logging
from typing import Any
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from .api_schemas import STTRequest

logger = logging.getLogger(__name__)


def register_voice_input(
    app: FastAPI, resolve_user: Callable[[Request], Awaitable[str | None]]
) -> None:
    @app.post("/api/stt")
    async def api_stt(req: STTRequest, request: Request) -> Any:
        """Transcribe his recording: ``{"audio": base64}`` -> ``{"text": ...}``."""
        user_id = await resolve_user(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        from .voice_client import (
            VOICE_INPUT_OFF_MESSAGE,
            VOICE_OFF_MESSAGE,
            SpeechToTextError,
            transcribe,
            voice_enabled,
            voice_input_enabled,
        )

        if not voice_enabled():
            return JSONResponse({"error": VOICE_OFF_MESSAGE}, status_code=503)
        if not voice_input_enabled():
            # No Fish key: nothing can transcribe his microphone.
            return JSONResponse({"error": VOICE_INPUT_OFF_MESSAGE}, status_code=503)
        # The PWA treats any 200 as a transcript, so every failure is a
        # non-200 with {"error": ...} — and never a 401, which logs him out.
        try:
            return await transcribe(req.audio, language=req.language)
        except SpeechToTextError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)
        except Exception as exc:
            logger.warning("STT failed: %s", type(exc).__name__)
            return JSONResponse({"error": "Voice service unavailable"}, status_code=503)
