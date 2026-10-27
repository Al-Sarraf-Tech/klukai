"""Voice letters — async JP voice notes Klukai leaves while the Commander is away.

When reflection-on-return composes a greeting, it can also be synthesized to a
``.wav`` and stored as a "voice letter" the Commander can play back later. This
module owns the synthesis + persistence (``save_voice_note``) and the playback
helpers (``get_voice_note``, ``latest_voice_note``, ``mark_played``).

Design:
- Audio bytes are produced through ``voice_client.post_tts`` (Fish Audio),
  exactly as the chat WS path and ``/api/tts`` do. The request shape mirrors
  ``/api/tts``: ``{"text": ..., "language": ...}``. ``language`` is the
  language of the text being read; her letters are English, so it defaults
  to ``"en"`` — the same thing the PWA sends.
- The WAV is written under ``AUDIO_DIR`` (``/audio`` volume), named ``{uuid}.wav``,
  mirroring how memory_archive writes image files under ``IMAGES_DIR``.
- A row lands in ``companion_voice_notes`` (migration 151), scoped per user.
- What she reads aloud is the text minus stage directions ("(she smiles)",
  "*nods*"), clipped at a sentence end; the full text is stored as the prompt.

TEXT-ONLY TODAY: ``voice_letters_enabled()`` is false, so no letter is
synthesized at all. The PWA plays a ``voice_letter``'s audio unasked, and she
speaks only when he taps, so ``save_voice_note`` returns ``None`` and the
greeting arrives as text. Saved letters still play back.

FAIL-SOFT: every failure mode — voice service down/non-200, disk write error,
DB insert error — is logged at WARNING and returns ``None`` (no row, no partial
state the caller can mistake for success). The reflection hook falls back to the
plain text greeting whenever this returns ``None``.
"""

from __future__ import annotations
from typing import Any


import asyncio
import logging
import os
import uuid
from pathlib import Path

from .db import get_pool
from .helpers import strip_actions_for_tts, tts_clip

logger = logging.getLogger(__name__)

# Volume mount for synthesized voice letters. Matches the IMAGES_DIR pattern in
# memory_archive.py — a single env-overridable Path resolved at import time.
AUDIO_DIR = Path(os.environ.get("AUDIO_DIR", "/audio"))

# The language of the TEXT. Her letters are written in English, like everything
# she says, so they are read as "en" (what the PWA sends too).
DEFAULT_VOICE_LANGUAGE = os.environ.get("VOICE_LANGUAGE", "en")

# /api/tts caps a line at 500 chars. Match that so a long reflective greeting
# never fails synthesis (the text greeting still carries in full).
_MAX_TTS_CHARS = 500


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
      1. Synthesize ``{"text", "language"}`` with Fish. Non-200 or an
         error → ``None``.
      2. Write the returned WAV bytes to ``AUDIO_DIR/{uuid}.wav``.
      3. INSERT a ``companion_voice_notes`` row scoped to ``user_id``.

    Returns:
        The voice-note id (UUID string) on success, else ``None``.
    """
    if not text or not text.strip():
        return None

    from .voice_client import voice_letters_enabled

    if not voice_letters_enabled():
        # The PWA auto-plays a letter's audio and she speaks only when he
        # taps. No synthesis and no voice_id; the greeting goes out as text.
        logger.info("Voice letter for %s stays text-only", user_id)
        return None

    # Stage directions are not read aloud; a letter that is nothing but
    # directions has no voice to give it, so there is no letter.
    speech = tts_clip(strip_actions_for_tts(text), _MAX_TTS_CHARS)
    if not speech:
        logger.info("Voice letter for %s had no speakable text — skipped", user_id)
        return None

    note_id = str(uuid.uuid4())
    filename = f"{note_id}.wav"

    # 1. Synthesize via Fish (mirror /api/tts request shape).
    try:
        from .voice_client import post_tts

        r = await post_tts({"text": speech, "language": language})
        if r.status_code != 200:
            logger.warning(
                "Voice letter synth failed (HTTP %s) for %s — falling back to text",
                r.status_code,
                user_id,
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
        logger.warning(
            "Voice letter write failed for %s (%s): %s", user_id, filename, e
        )
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
                (
                    note_id,
                    filename,
                    text.strip(),
                    kind,
                    mood,
                    affection_level,
                    conversation_id,
                    user_id,
                ),
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
        user_id,
        note_id[:8],
        kind,
        len(text),
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
            row = await (
                await conn.execute(
                    "SELECT audio_filename FROM companion_voice_notes "
                    "WHERE id = %s AND user_id = %s",
                    (note_id, user_id),
                )
            ).fetchone()
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


async def latest_voice_note(user_id: str) -> dict[str, Any] | None:
    """Return metadata for the user's most recent voice letter, or ``None``.

    Used by the client to surface an unheard letter. Returns a small dict (no
    audio bytes — fetch those via ``get_voice_note`` / the audio endpoint).
    Fails closed to ``None`` on DB error.
    """
    try:
        pool = get_pool()
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT id, kind, mood, affection_level, played_at, created_at "
                    "FROM companion_voice_notes WHERE user_id = %s "
                    "ORDER BY created_at DESC LIMIT 1",
                    (user_id,),
                )
            ).fetchone()
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
