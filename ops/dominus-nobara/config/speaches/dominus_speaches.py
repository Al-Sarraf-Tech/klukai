"""Harden pinned Speaches 0.8.3 for bounded, CPU-only fleet service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import inspect
import os
from typing import Any


MAX_MODEL_IDLE_TTL_SECONDS = 900
# Speaches 0.8.3 disposes models from a ``threading.Timer`` scheduled at the
# configured TTL. Keep a server-owned five-second margin so timer scheduling
# jitter cannot push actual residency beyond the public 900-second policy.
MODEL_IDLE_SAFETY_CUTOFF_SECONDS = 895
TRANSCRIPTION_PATHS = {
    "/v1/audio/transcriptions",
    "/v1/audio/translations",
}
BLOCKED_PATH_PREFIXES = (
    "/v1/audio/speech/timestamps",
    "/v1/audio/diarization",
    "/v1/realtime",
    "/v1/chat/completions",
)


def validate_ttl_contract(environment: dict[str, str] | None = None) -> int:
    values = os.environ if environment is None else environment
    try:
        shared_ttl = int(values["WHISPER__TTL"])
        tts_ttl = int(values["DOMINUS_SPEACHES_TTS_TTL"])
    except (KeyError, ValueError) as error:
        raise RuntimeError("Speaches STT/TTS TTL contract is absent or invalid") from error
    if not 1 <= shared_ttl <= MODEL_IDLE_SAFETY_CUTOFF_SECONDS:
        raise RuntimeError(
            "Speaches shared model TTL exceeds the 895-second runtime cutoff "
            "under the 900-second policy ceiling"
        )
    if tts_ttl != shared_ttl:
        raise RuntimeError("pinned Speaches 0.8.3 requires matching STT and TTS TTLs")
    return shared_ttl


def _force_vad_disabled(
    call: Callable[..., Any],
) -> Callable[..., Awaitable[Any]]:
    async def no_vad_call(**arguments: Any) -> Any:
        arguments["vad_filter"] = False
        result = call(**arguments)
        if inspect.isawaitable(result):
            return await result
        return result

    return no_vad_call


def harden_application(application: Any) -> Any:
    retained_routes = []
    for route in application.router.routes:
        path = getattr(route, "path", "")
        if isinstance(path, str) and path.startswith(BLOCKED_PATH_PREFIXES):
            continue
        if path in TRANSCRIPTION_PATHS:
            dependant = getattr(route, "dependant", None)
            call = getattr(dependant, "call", None)
            if call is None:
                raise RuntimeError(f"cannot enforce no-VAD policy for {path}")
            wrapped = _force_vad_disabled(call)
            dependant.call = wrapped
            route.endpoint = wrapped
        retained_routes.append(route)
    application.router.routes[:] = retained_routes
    return application


def create_app() -> Any:
    validate_ttl_contract()
    from speaches.main import create_app as create_upstream_app

    return harden_application(create_upstream_app())
