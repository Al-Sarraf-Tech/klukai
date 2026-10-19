"""The Commander's voice settings: which language she speaks when he taps."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api_schemas import VoiceSettingsRequest

logger = logging.getLogger(__name__)


def register_voice_settings(
    app: FastAPI, resolve_user: Callable[[Request], Awaitable[str | None]]
) -> None:
    @app.get("/api/voice/settings")
    async def api_voice_settings(request: Request) -> Any:
        user_id = await resolve_user(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        from .voice_language import VOICE_LANGUAGES, load_voice_language

        try:
            language = await load_voice_language(user_id)
        except Exception as exc:
            logger.warning("Voice settings read failed: %s", type(exc).__name__)
            return JSONResponse({"error": "Settings unavailable"}, status_code=503)
        return JSONResponse(
            {"language": language, "languages": list(VOICE_LANGUAGES)},
            headers={"Cache-Control": "no-store"},
        )

    @app.put("/api/voice/settings")
    async def api_voice_settings_update(
        req: VoiceSettingsRequest, request: Request
    ) -> Any:
        user_id = await resolve_user(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        from .voice_language import VOICE_LANGUAGES, save_voice_language

        try:
            language = await save_voice_language(user_id, req.language)
        except Exception as exc:
            logger.warning("Voice settings write failed: %s", type(exc).__name__)
            return JSONResponse({"error": "Settings unavailable"}, status_code=503)
        return JSONResponse(
            {"language": language, "languages": list(VOICE_LANGUAGES)},
            headers={"Cache-Control": "no-store"},
        )
