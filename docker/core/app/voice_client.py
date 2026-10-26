"""Backend-specific admission and audio normalization for companion voice.

Backends (``VOICE_BACKEND``):

- ``dominus`` (default): remote XTTS behind the shared LM gate + GPU lease.
- ``amarillo-xpu``: the native Arc worker, serialized by a local lock.
- ``fish``: the Fish Audio cloud API speaking with the community "Klukai"
  model. No gate and no lease: nothing local is loaded. TTS carries the
  free model header and a non-free TTS model is refused before anything
  is sent. Fish
  speaks only when he taps. Funded-account STT has a separate explicit switch;
  voice letters remain text-only.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Any

import httpx

from .dependencies import http_request

from .gpu_lease import gpu_lease, gpu_lease_capability_headers

_local_gate = asyncio.Lock()
LOCAL_TTS_DEADLINE = 600.0
MAX_AUDIO_BYTES = 6 * 1024 * 1024
logger = logging.getLogger(__name__)


_OFF = {"false", "0", "no", "off"}
VOICE_OFF_MESSAGE = "Voice is turned off"
VOICE_INPUT_OFF_MESSAGE = "Voice input is off"


def voice_enabled() -> bool:
    """The owner's switch. Off means core never sends voice work anywhere."""
    return os.environ.get("KLUKAI_VOICE_ENABLED", "true").strip().lower() not in _OFF


def voice_off_response() -> httpx.Response:
    return httpx.Response(503, json={"error": VOICE_OFF_MESSAGE})


def local_voice_enabled() -> bool:
    return os.environ.get("VOICE_BACKEND", "dominus") == "amarillo-xpu"


def fish_voice_enabled() -> bool:
    """True when her voice is the Fish Audio cloud model."""
    return os.environ.get("VOICE_BACKEND", "dominus").strip().lower() == "fish"


def voice_input_enabled() -> bool:
    """Fish transcription requires a separate explicit funded-account opt-in."""
    if not voice_enabled():
        return False
    if fish_voice_enabled():
        return os.environ.get("KLUKAI_FISH_STT_ENABLED", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        } and bool(os.environ.get("FISH_API_KEY", "").strip())
    return True


def voice_letters_enabled() -> bool:
    """Proactive voice letters. The PWA auto-plays a letter's audio, and with
    Fish she speaks only when he taps, so letters stay text-only."""
    return voice_enabled() and not fish_voice_enabled()


def async_voice_jobs_enabled() -> bool:
    """Keep Arc queued; allow other backends to opt into submit/poll speech."""
    return local_voice_enabled() or os.environ.get(
        "VOICE_ASYNC_JOBS", "false"
    ).strip().lower() in {
        "true",
        "1",
        "yes",
        "on",
    }


def tts_timeout(remote_default: float = 60.0) -> float:
    return LOCAL_TTS_DEADLINE if async_voice_jobs_enabled() else remote_default


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
            async with httpx.AsyncClient(
                timeout=10.0, trust_env=False, follow_redirects=False
            ) as cleanup:
                # Cleanup must reach the worker even after synthesis trips the
                # voice breaker; it never admits inference or a fallback.
                response = await cleanup.post(
                    f"{voice_url()}/unload", headers=voice_auth_headers()
                )
                response.raise_for_status()
    except Exception as exc:
        logger.warning("Voice cancellation cleanup unavailable: %s", type(exc).__name__)


# ── Fish Audio ────────────────────────────────────────────────────────────

FISH_TTS_URL = "https://api.fish.audio/v1/tts"
# Public community model "Klukai" by Danastico (owner decision 2026-10-19).
DEFAULT_FISH_VOICE_ID = "47fc35b7311944bc9fa238ed856de1f3"
# Free through 2026-11-30. A missing or different header bills a paid model.
DEFAULT_FISH_MODEL = "s2.1-pro-free"
FISH_LANGUAGES = frozenset({"en", "ja"})
FISH_TTS_TIMEOUT = 60.0
FISH_OFF_MESSAGE = "Voice is not configured"
_FISH_FAILED_MESSAGE = "Voice service unavailable"


def fish_configured() -> bool:
    """A key, a free model and a voice are set, so a tap can reach Fish.

    Checks configuration only; nothing is sent (health must not spend).
    """
    model = os.environ.get("KLUKAI_FISH_MODEL", DEFAULT_FISH_MODEL).strip()
    return bool(
        os.environ.get("FISH_API_KEY", "").strip()
        and model.endswith("-free")
        and os.environ.get("KLUKAI_FISH_VOICE_ID", DEFAULT_FISH_VOICE_ID).strip()
    )


def _fish_response(status: int, message: str) -> httpx.Response:
    # Never echo Fish's body: errors can quote the account or the request.
    return httpx.Response(
        status,
        json={"error": message},
        request=httpx.Request("POST", FISH_TTS_URL),
    )


def _is_wav(audio: bytes) -> bool:
    return len(audio) > 12 and audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"


async def _post_fish_tts(
    payload: dict[str, Any], *, timeout: float | None = None
) -> httpx.Response:
    """One Fish synthesis. No LM gate, no GPU lease; WAV bytes or a non-200."""
    key = os.environ.get("FISH_API_KEY", "").strip()
    model = os.environ.get("KLUKAI_FISH_MODEL", DEFAULT_FISH_MODEL).strip()
    voice_id = os.environ.get("KLUKAI_FISH_VOICE_ID", DEFAULT_FISH_VOICE_ID).strip()
    if not key or not model or not voice_id:
        logger.warning("Fish voice refused: key, model or voice id is not set")
        return _fish_response(503, FISH_OFF_MESSAGE)
    if not model.endswith("-free"):
        # The account has no credit and must never be billed: refuse a paid
        # model here rather than discover it in Fish's 402.
        logger.error("Fish voice refused: model %r is not a free model", model)
        return _fish_response(503, FISH_OFF_MESSAGE)
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return _fish_response(400, "No speakable text")
    language = payload.get("language")
    body = {
        "text": text,
        "reference_id": voice_id,
        "format": "wav",
        "sample_rate": 24000,
        "latency": "normal",
        "language": language if language in FISH_LANGUAGES else "en",
    }
    headers = {"Authorization": f"Bearer {key}", "model": model}
    deadline = min(timeout or FISH_TTS_TIMEOUT, FISH_TTS_TIMEOUT)
    async with httpx.AsyncClient(
        timeout=deadline, trust_env=False, follow_redirects=False
    ) as fish:
        response = await http_request(
            "voice",
            fish,
            "post",
            FISH_TTS_URL,
            json=body,
            headers=headers,
            timeout=deadline,
        )
    if response.status_code != 200:
        logger.warning("Fish TTS HTTP %s", response.status_code)
        return _fish_response(502, _FISH_FAILED_MESSAGE)
    audio = response.content
    if not audio or len(audio) > MAX_AUDIO_BYTES or not _is_wav(audio):
        raise ValueError("Invalid voice audio result")
    # Preserve the binary WAV contract; the request carries no credentials.
    return httpx.Response(
        200,
        content=audio,
        headers={"Content-Type": "audio/wav"},
        request=httpx.Request("POST", FISH_TTS_URL),
    )


async def post_leased_tts(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
    *,
    timeout: float | None = None,
) -> httpx.Response:
    """Serialize Arc synthesis, call Fish directly, or take Dominus's GPU lease."""

    # Import lazily to avoid a helpers -> image_gen -> llm_router import cycle.
    from .llm_router import get_lm_gate
    from .helpers import voice_auth_headers

    if not voice_enabled():
        # No gate wait and no GPU lease: the voice host may be off entirely.
        return voice_off_response()

    if fish_voice_enabled():
        # A cloud model: nothing local to admit, so no gate and no lease.
        return await _post_fish_tts(payload, timeout=timeout)

    if local_voice_enabled():
        # The native supervisor enforces the Arc thermal/worker boundary.
        # Every core TTS path shares this lock, including archive and WS calls.
        async with asyncio.timeout(LOCAL_TTS_DEADLINE):
            async with _local_gate:
                try:
                    response = await http_request(
                        "voice",
                        client,
                        "post",
                        f"{voice_url()}/tts",
                        json=payload,
                        headers=voice_auth_headers(),
                        timeout=LOCAL_TTS_DEADLINE,
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
                if (
                    not isinstance(encoded, str)
                    or len(encoded) > MAX_AUDIO_BYTES * 4 // 3 + 4
                ):
                    raise ValueError("Invalid voice audio result")
                audio = base64.b64decode(encoded, validate=True)
                if not audio or len(audio) > MAX_AUDIO_BYTES:
                    raise ValueError("Invalid voice audio result")
                # Preserve the binary WAV contract used by existing callers.
                return httpx.Response(
                    200,
                    content=audio,
                    headers={"Content-Type": "audio/wav"},
                    request=response.request,
                )

    async with get_lm_gate():
        async with gpu_lease("companion-voice") as lease:
            headers = {
                **voice_auth_headers(),
                **gpu_lease_capability_headers(lease),
            }
            return await http_request(
                "voice",
                client,
                "post",
                f"{voice_url()}/tts",
                json=payload,
                headers=headers,
                timeout=timeout,
            )


async def post_fish_stt(encoded_audio: str) -> httpx.Response:
    """Bounded, user-requested transcription; no retries or local fallback."""
    if not fish_voice_enabled() or not voice_input_enabled():
        return _fish_response(503, VOICE_INPUT_OFF_MESSAGE)
    try:
        audio = base64.b64decode(encoded_audio, validate=True)
    except (ValueError, TypeError):
        return _fish_response(400, "Invalid audio")
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        return _fish_response(400, "Invalid audio")
    async with httpx.AsyncClient(
        timeout=30.0, trust_env=False, follow_redirects=False
    ) as client:
        response = await http_request(
            "voice",
            client,
            "post",
            "https://api.fish.audio/v1/asr",
            headers={
                "Authorization": f"Bearer {os.environ['FISH_API_KEY']}",
                "model": "transcribe-1-pro",
            },
            files={"audio": ("recording.audio", audio, "application/octet-stream")},
            data={
                "ignore_timestamps": "true",
                "diarize": "false",
                "tag_audio_events": "false",
            },
            timeout=30.0,
        )
    if response.status_code != 200:
        logger.warning("Fish STT HTTP %s", response.status_code)
        return _fish_response(502, _FISH_FAILED_MESSAGE)
    body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("text"), str):
        return _fish_response(502, "Transcription failed")
    return httpx.Response(200, json={"text": body["text"]}, request=response.request)
