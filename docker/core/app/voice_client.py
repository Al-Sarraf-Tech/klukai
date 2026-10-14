"""Backend-specific GPU admission and audio normalization for companion voice."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Any

import httpx

from .gpu_lease import gpu_lease, gpu_lease_capability_headers

_local_gate = asyncio.Lock()
LOCAL_TTS_DEADLINE = 600.0
MAX_AUDIO_BYTES = 6 * 1024 * 1024
logger = logging.getLogger(__name__)


def local_voice_enabled() -> bool:
    return os.environ.get("VOICE_BACKEND", "dominus") == "amarillo-xpu"


def tts_timeout(remote_default: float = 60.0) -> float:
    return LOCAL_TTS_DEADLINE if local_voice_enabled() else remote_default


def voice_url() -> str:
    return os.environ.get("VOICE_URL", "http://100.107.121.5:8301").rstrip("/")


def stt_url() -> str:
    """Arc currently serves TTS only; keep transcription on the existing host."""
    default = "http://100.107.121.5:8301" if local_voice_enabled() else voice_url()
    return os.environ.get("VOICE_STT_URL", default).rstrip("/")


async def _unload_local_voice() -> None:
    """Confirm native worker reap before allowing another core synthesis."""
    from .helpers import voice_auth_headers

    try:
        async with asyncio.timeout(10.0):
            async with httpx.AsyncClient(timeout=10.0, trust_env=False, follow_redirects=False) as cleanup:
                response = await cleanup.post(f"{voice_url()}/unload", headers=voice_auth_headers())
                response.raise_for_status()
    except Exception as exc:
        logger.warning("Voice cancellation cleanup unavailable: %s", type(exc).__name__)


async def post_leased_tts(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
    *,
    timeout: float | None = None,
) -> httpx.Response:
    """Serialize Arc synthesis, or retain Dominus's shared GPU lease boundary."""

    # Import lazily to avoid a helpers -> image_gen -> llm_router import cycle.
    from .llm_router import get_lm_gate
    from .helpers import voice_auth_headers

    if local_voice_enabled():
        # The native supervisor enforces the Arc thermal/worker boundary.
        # Every core TTS path shares this lock, including archive and WS calls.
        async with asyncio.timeout(LOCAL_TTS_DEADLINE):
            async with _local_gate:
                try:
                    response = await client.post(
                        f"{voice_url()}/tts", json=payload,
                        headers=voice_auth_headers(), timeout=LOCAL_TTS_DEADLINE,
                    )
                except (asyncio.CancelledError, httpx.TimeoutException):
                    # Closing TCP alone races the supervisor's disconnect
                    # poll. Hold admission until its authenticated unload has
                    # reaped the worker, including on application shutdown.
                    cleanup = asyncio.create_task(_unload_local_voice())
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                    raise
                if response.status_code != 200:
                    return response
                encoded = response.json().get("audio")
                if not isinstance(encoded, str) or len(encoded) > MAX_AUDIO_BYTES * 4 // 3 + 4:
                    raise ValueError("Invalid voice audio result")
                audio = base64.b64decode(encoded, validate=True)
                if not audio or len(audio) > MAX_AUDIO_BYTES:
                    raise ValueError("Invalid voice audio result")
                # Preserve the binary WAV contract used by existing callers.
                return httpx.Response(
                    200, content=audio, headers={"Content-Type": "audio/wav"},
                    request=response.request,
                )

    async with get_lm_gate():
        async with gpu_lease("companion-voice") as lease:
            headers = {
                **voice_auth_headers(),
                **gpu_lease_capability_headers(lease),
            }
            return await client.post(
                f"{voice_url()}/tts",
                json=payload,
                headers=headers,
                timeout=timeout,
            )
