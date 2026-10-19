"""Authenticated voice-transcription proxy and its fail-soft wire responses."""

from __future__ import annotations
from collections.abc import Awaitable, Callable
import logging
from typing import Any
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from .api_schemas import STTRequest
from .dependencies import http_request

logger = logging.getLogger(__name__)


def register_voice_input(
    app: FastAPI, resolve_user: Callable[[Request], Awaitable[str | None]]
) -> None:
    @app.post("/api/stt")
    async def api_stt(req: STTRequest, request: Request) -> Any:
        """Proxy STT request to companion-voice."""
        user_id = await resolve_user(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        from .voice_client import VOICE_OFF_MESSAGE, voice_enabled

        if not voice_enabled():
            return JSONResponse({"error": VOICE_OFF_MESSAGE}, status_code=503)
        try:
            from .helpers import voice_auth_headers
            from .voice_client import stt_url

            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await http_request(
                    "voice",
                    client,
                    "post",
                    f"{stt_url()}/stt",
                    json={"audio": req.audio},
                    headers=voice_auth_headers(),
                )
                # The PWA treats any 200 as a transcript, so an upstream
                # failure must not come back as a 200 — it would silently do
                # nothing. Status and {"error": ...} bodies both become a 502.
                if r.status_code != 200:
                    logger.warning("STT upstream HTTP %s", r.status_code)
                    return JSONResponse(
                        {"error": "Voice service unavailable"}, status_code=502
                    )
                body = r.json()
        except Exception:
            return JSONResponse({"error": "Voice service unavailable"}, status_code=503)
        if not isinstance(body, dict):
            logger.warning("STT upstream returned a non-object body")
            return JSONResponse({"error": "Transcription failed"}, status_code=502)
        if "error" in body:
            logger.warning("STT upstream error: %s", str(body["error"])[:200])
            return JSONResponse({"error": "Transcription failed"}, status_code=502)
        return body
