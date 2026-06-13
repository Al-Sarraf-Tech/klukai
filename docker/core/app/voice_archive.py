"""Voice letters — async JP voice notes Klukai leaves while the Commander is away.

When reflection-on-return composes a greeting, it can also be synthesized to a
``.wav`` and stored as a "voice letter" the Commander can play back later. This
module owns the synthesis + persistence (``save_voice_note``) and the playback
helpers (``get_voice_note``, ``latest_voice_note``, ``mark_played``).

Design:
- Audio bytes are produced by POSTing to the companion-voice service ``/tts``,
  exactly as the chat WS path and ``/api/tts`` do (VOICE_URL + voice_auth_headers).
  The request shape mirrors ``/api/tts``: ``{"text": ..., "language": ...}``.
  Klukai speaks Japanese (the project default), so ``language`` defaults to
  ``"ja"`` here — the server-configured JP voice the WS path relies on.
- The WAV is written under ``AUDIO_DIR`` (``/audio`` volume), named ``{uuid}.wav``,
  mirroring how memory_archive writes image files under ``IMAGES_DIR``.
- A row lands in ``companion_voice_notes`` (migration 151), scoped per user.

FAIL-SOFT: every failure mode — voice service down/non-200, disk write error,
DB insert error — is logged at WARNING and returns ``None`` (no row, no partial
state the caller can mistake for success). The reflection hook falls back to the
plain text greeting whenever this returns ``None``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path

import httpx

from .db import get_pool

logger = logging.getLogger(__name__)

# Volume mount for synthesized voice letters. Matches the IMAGES_DIR pattern in
# memory_archive.py — a single env-overridable Path resolved at import time.
AUDIO_DIR = Path(os.environ.get("AUDIO_DIR", "/audio"))

# Klukai's voice is Japanese (project default — JP voice ONLY). The chat WS path
# omits `language` and relies on the voice service's configured default; here we
# pass it explicitly so a server-initiated letter is unambiguously her JP voice.
DEFAULT_VOICE_LANGUAGE = os.environ.get("VOICE_LANGUAGE", "ja")

# XTTS rejects very long inputs; /api/tts caps at 500 chars. Match that so a long
# reflective greeting never fails synthesis (the text greeting still carries full).
_MAX_TTS_CHARS = 500


def _voice_url() -> str:
    """Resolve the voice service base URL (same default as the chat WS / TTS paths)."""
    return os.environ.get("VOICE_URL", "http://companion-voice:8301")


async def save_voice_note(
    text: str,
    user_id: str = "jalsarraf",
    kind: str = "reflection",
    mood: str | None = None,
    affection_level: int = 0,
    conversation_id: str | None = None,
    language: str = DEFAULT_VOICE_LANGUAGE,
) -> str | None:
    """Synthesize ``text`` to a JP voice letter, store it, and return its id.

    Steps (all fail-soft → return ``None`` on any error, leaving no row):
      1. POST ``{VOICE_URL}/tts`` with ``{"text", "language"}`` + voice auth
         headers (same as ``/api/tts``). Non-200 → ``None``.
      2. Write the returned WAV bytes to ``AUDIO_DIR/{uuid}.wav``.
      3. INSERT a ``companion_voice_notes`` row scoped to ``user_id``.

    Returns:
        The voice-note id (UUID string) on success, else ``None``.
    """
    if not text or not text.strip():
        return None

    note_id = str(uuid.uuid4())
    filename = f"{note_id}.wav"

    # 1. Synthesize via the voice service (mirror /api/tts request shape).
    try:
        from .helpers import voice_auth_headers
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{_voice_url()}/tts",
                json={"text": text.strip()[:_MAX_TTS_CHARS], "language": language},
                headers=voice_auth_headers(),
            )
        if r.status_code != 200:
            logger.warning(
                "Voice letter synth failed (HTTP %s) for %s — falling back to text",
                r.status_code, user_id,
            )
            return None
        audio_bytes = r.content
    except Exception as e:
        logger.warning("Voice letter synth unavailable for %s: %s", user_id, e)
        return None

    if not audio_bytes:
        logger.warning("Voice letter synth returned empty audio for %s", user_id)
        return None

    # 2. Persist the WAV to the audio volume (offload the blocking write).
    try:
        path = AUDIO_DIR / filename
        await asyncio.to_thread(path.write_bytes, audio_bytes)
    except Exception as e:
        logger.warning("Voice letter write failed for %s (%s): %s", user_id, filename, e)
        return None

    # 3. Record the metadata row.
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO companion_voice_notes "
                "(id, audio_filename, text_prompt, kind, mood, affection_level, "
                "conversation_id, user_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (note_id, filename, text.strip(), kind, mood, affection_level,
                 conversation_id, user_id),
            )
            await conn.commit()
    except Exception as e:
        logger.warning("Voice letter row insert failed for %s: %s", user_id, e)
        # Best-effort cleanup of the orphaned WAV so disk doesn't leak on DB outage.
        try:
            await asyncio.to_thread((AUDIO_DIR / filename).unlink, True)
        except Exception:
            pass
        return None

    logger.info(
        "Voice letter saved for %s: id=%s kind=%s len=%d",
        user_id, note_id[:8], kind, len(text),
    )
    return note_id


async def get_voice_note(note_id: str, user_id: str) -> tuple[bytes, str] | None:
    """Read a voice letter's WAV bytes, enforcing ownership.

    Returns ``(audio_bytes, filename)`` when the note belongs to ``user_id`` and
    the file exists on disk; otherwise ``None`` (not found / not owner / missing
    file / DB error — the caller maps this to a 404).
    """
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT audio_filename FROM companion_voice_notes "
                "WHERE id = %s AND user_id = %s",
                (note_id, user_id),
            )).fetchone()
        if not row or not row[0]:
            return None
        filename = row[0]
        path = AUDIO_DIR / filename
        if not path.exists():
            return None
        audio = await asyncio.to_thread(path.read_bytes)
        return audio, filename
    except Exception as e:
        logger.error("Failed to read voice letter %s: %s", note_id, e)
        return None


async def latest_voice_note(user_id: str) -> dict | None:
    """Return metadata for the user's most recent voice letter, or ``None``.

    Used by the client to surface an unheard letter. Returns a small dict (no
    audio bytes — fetch those via ``get_voice_note`` / the audio endpoint).
    Fails closed to ``None`` on DB error.
    """
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT id, kind, mood, affection_level, played_at, created_at "
                "FROM companion_voice_notes WHERE user_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            )).fetchone()
        if not row:
            return None
        return {
            "id": str(row[0]),
            "kind": row[1],
            "mood": row[2],
            "affection_level": row[3],
            "played": row[4] is not None,
            "played_at": row[4].isoformat() if row[4] else None,
            "created_at": row[5].isoformat() if row[5] else None,
        }
    except Exception as e:
        logger.error("Failed to read latest voice letter for %s: %s", user_id, e)
        return None


async def mark_played(note_id: str, user_id: str) -> bool:
    """Stamp ``played_at`` on a voice letter (idempotent — only sets it once).

    Scoped to ``user_id``. Returns ``True`` if a row was updated (the note exists,
    belongs to the user, and was previously unplayed), else ``False``.
    """
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            result = await conn.execute(
                "UPDATE companion_voice_notes SET played_at = NOW() "
                "WHERE id = %s AND user_id = %s AND played_at IS NULL",
                (note_id, user_id),
            )
            await conn.commit()
        return bool(getattr(result, "rowcount", 0) and result.rowcount > 0)
    except Exception as e:
        logger.warning("Failed to mark voice letter %s played: %s", note_id, e)
        return False


__all__ = [
    "AUDIO_DIR",
    "save_voice_note",
    "get_voice_note",
    "latest_voice_note",
    "mark_played",
]
