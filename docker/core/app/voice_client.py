"""Companion voice on Fish Audio: her speech and his transcription.

Both directions are cloud calls with ``FISH_API_KEY``; nothing runs locally
and nothing takes a GPU lease. ``KLUKAI_VOICE_ENABLED`` is the owner's
switch: off, core sends no voice work anywhere.

- Text-to-speech: the public community "Klukai" model. Every request carries
  a free model header, and a model that is not free is refused before
  anything is sent. She speaks only when he taps: no voice letters.
- Speech-to-text: ``transcribe-1-pro``, pay-as-you-go (owner authorized
  2026-10-26).

Neither path logs audio, transcripts, keys or Fish's response bodies.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
from typing import Any

import httpx

from .dependencies import http_request, http_stream

MAX_AUDIO_BYTES = 6 * 1024 * 1024
logger = logging.getLogger(__name__)


_OFF = {"false", "0", "no", "off"}
_ON = {"true", "1", "yes", "on"}
VOICE_OFF_MESSAGE = "Voice is turned off"
VOICE_INPUT_OFF_MESSAGE = "Voice input is off"


def voice_enabled() -> bool:
    """The owner's switch. Off means core never sends voice work anywhere."""
    return os.environ.get("KLUKAI_VOICE_ENABLED", "true").strip().lower() not in _OFF


def voice_off_response() -> httpx.Response:
    return _fish_response(503, VOICE_OFF_MESSAGE)


def fish_voice_enabled() -> bool:
    """Fish Audio is her only voice.

    Kept because the health report still asks which backend speaks; remove it
    with that last caller.
    """
    return True


def voice_input_enabled() -> bool:
    """Microphone transcription: the switch is on and Fish has a key."""
    return voice_enabled() and fish_stt_configured()


def voice_letters_enabled() -> bool:
    """Proactive voice letters stay text-only.

    The PWA auto-plays a letter's audio, and she speaks only when he taps.
    """
    return False


def async_voice_jobs_enabled() -> bool:
    """Opt-in submit/poll speech (``VOICE_ASYNC_JOBS``) for long lines."""
    return os.environ.get("VOICE_ASYNC_JOBS", "false").strip().lower() in _ON


# ── Fish Audio text-to-speech ─────────────────────────────────────────────

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


async def post_tts(
    payload: dict[str, Any], *, timeout: float | None = None
) -> httpx.Response:
    """One Fish synthesis of ``{"text", "language"}``: WAV bytes or a non-200.

    Raises ``ValueError`` when a 200 is not a WAV within ``MAX_AUDIO_BYTES``.
    """
    if not voice_enabled():
        return voice_off_response()
    key = os.environ.get("FISH_API_KEY", "").strip()
    model = os.environ.get("KLUKAI_FISH_MODEL", DEFAULT_FISH_MODEL).strip()
    voice_id = os.environ.get("KLUKAI_FISH_VOICE_ID", DEFAULT_FISH_VOICE_ID).strip()
    if not key or not model or not voice_id:
        logger.warning("Fish voice refused: key, model or voice id is not set")
        return _fish_response(503, FISH_OFF_MESSAGE)
    if not model.endswith("-free"):
        # Speech stays on the free model: refuse a paid one here rather than
        # bill it or discover it in Fish's 402.
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


# ── Fish Audio speech-to-text ─────────────────────────────────────────────

FISH_ASR_URL = "https://api.fish.audio/v1/asr"
# Written exactly: any other value silently falls back to transcribe-1.
FISH_ASR_MODEL = "transcribe-1-pro"
# The apps stop waiting at 35 s, so answer (or give up) before they do.
FISH_STT_TIMEOUT = 30.0
# STTRequest caps the base64 at 8,000,000 characters (~6 MB of audio).
MAX_STT_AUDIO_BYTES = 6 * 1024 * 1024
# A transcript of a one-minute take is a few kilobytes.
MAX_STT_RESPONSE_BYTES = 256 * 1024
_MAX_STT_ERROR_BYTES = 4 * 1024
STT_FAILED_MESSAGE = "Transcription failed"
STT_BUSY_MESSAGE = "Voice service busy"
# Fish's documented error codes: safe to log, unlike the rest of its body.
_STT_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "invalid_parameter",
        "invalid_audio",
        "audio_too_long",
        "audio_too_short",
        "request_too_large",
        "unsupported_media_type",
        "upstream_rejected",
        "internal_error",
        "upstream_unavailable",
        "upstream_timeout",
        "excessive_repetition",
        "diarization_failed",
    }
)


class SpeechToTextError(Exception):
    """A failed transcription; ``status`` and ``message`` are safe to return."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def fish_stt_configured() -> bool:
    """A key is set, so his microphone can reach Fish. Nothing is sent."""
    return bool(os.environ.get("FISH_API_KEY", "").strip())


def _stt_language(language: object) -> str | None:
    """``en``/``ja`` go as a hint; anything else lets Fish detect it."""
    if isinstance(language, str):
        code = language.strip().lower()
        if code in FISH_LANGUAGES:
            return code
    return None


def _decode_audio(audio_b64: str) -> bytes:
    try:
        audio = base64.b64decode(audio_b64)
    except ValueError as exc:  # binascii.Error, or non-ASCII text
        raise SpeechToTextError(400, "Invalid audio") from exc
    if not audio:
        raise SpeechToTextError(400, "Invalid audio")
    if len(audio) > MAX_STT_AUDIO_BYTES:
        raise SpeechToTextError(413, "Recording is too large")
    return audio


async def _read_capped(response: httpx.Response, limit: int) -> bytes | None:
    """The body, or ``None`` once it grows past ``limit``."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


def _error_code(body: bytes | None) -> str:
    """Fish's error ``code`` when it is a documented one, else ``"-"``."""
    try:
        payload = json.loads(body or b"")
    except (ValueError, RecursionError):
        return "-"
    code = payload.get("code") if isinstance(payload, dict) else None
    return code if isinstance(code, str) and code in _STT_ERROR_CODES else "-"


def _transcript(body: bytes | None) -> dict[str, Any]:
    """Fish's reply in the shape the apps read: ``text`` (+ language, duration)."""
    if body is None:
        logger.warning("Fish speech-to-text reply exceeded the size cap")
        raise SpeechToTextError(502, STT_FAILED_MESSAGE)
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as exc:
        logger.warning("Fish speech-to-text reply was not JSON")
        raise SpeechToTextError(502, STT_FAILED_MESSAGE) from exc
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        logger.warning("Fish speech-to-text reply had no transcript")
        raise SpeechToTextError(502, STT_FAILED_MESSAGE)
    result: dict[str, Any] = {"text": text.strip()}
    code = payload.get("language_code")
    if isinstance(code, str) and code.isascii() and code.isalpha() and len(code) <= 3:
        result["language"] = code.lower()
    duration = payload.get("duration")
    if (
        isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and math.isfinite(duration)
        and duration >= 0
    ):
        result["duration"] = float(duration)
    return result


async def transcribe(audio_b64: str, *, language: str | None = None) -> dict[str, Any]:
    """His base64 recording to text with Fish ``transcribe-1-pro``.

    Returns ``{"text", "language"?, "duration"?}``, the shape the apps read.
    Raises :class:`SpeechToTextError` on every failure; the audio is sent
    once and never retried, logged or kept.
    """
    key = os.environ.get("FISH_API_KEY", "").strip()
    if not key:
        logger.warning("Fish speech-to-text refused: FISH_API_KEY is not set")
        raise SpeechToTextError(503, VOICE_INPUT_OFF_MESSAGE)
    audio = _decode_audio(audio_b64)
    form = {
        "ignore_timestamps": "true",
        # A plain transcript for chat: no "[laughter]" cues, no speaker marks.
        "tag_audio_events": "false",
        "diarize": "false",
    }
    hint = _stt_language(language)
    if hint is not None:
        form["language"] = hint
    headers = {"Authorization": f"Bearer {key}", "model": FISH_ASR_MODEL}
    try:
        async with asyncio.timeout(FISH_STT_TIMEOUT):
            async with httpx.AsyncClient(
                timeout=FISH_STT_TIMEOUT, trust_env=False, follow_redirects=False
            ) as fish:
                async with http_stream(
                    "voice",
                    fish,
                    "POST",
                    FISH_ASR_URL,
                    data=form,
                    files={"audio": ("audio", audio, "application/octet-stream")},
                    headers=headers,
                ) as response:
                    status = response.status_code
                    limit = (
                        MAX_STT_RESPONSE_BYTES
                        if status == 200
                        else _MAX_STT_ERROR_BYTES
                    )
                    body = await _read_capped(response, limit)
    except (httpx.HTTPError, TimeoutError) as exc:
        logger.warning("Fish speech-to-text unavailable: %s", type(exc).__name__)
        raise SpeechToTextError(503, _FISH_FAILED_MESSAGE) from exc
    if status != 200:
        logger.warning("Fish speech-to-text HTTP %s (%s)", status, _error_code(body))
        if status == 429:
            # The account is at its concurrency limit; nothing was transcribed.
            raise SpeechToTextError(503, STT_BUSY_MESSAGE)
        if status in {400, 413, 415}:
            # Fish could not use this recording (too short, too long, unreadable).
            raise SpeechToTextError(502, STT_FAILED_MESSAGE)
        raise SpeechToTextError(502, _FISH_FAILED_MESSAGE)
    return _transcript(body)
