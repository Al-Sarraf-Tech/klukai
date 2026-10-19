"""The language she speaks in, per Commander, and her Japanese lines.

Owner decision 2026-10-19: with the Fish voice she speaks English by default,
her reply exactly as written. A Commander can choose Japanese instead; then
the line is translated into her natural Japanese first, through the same LLM
path chat uses (``router.route(..., interactive=True)`` + ``router.stream``),
so no new provider is involved.

- The choice lives in ``companion_user_settings`` (migration 240). No row
  means English.
- Translations are cached in Redis by a hash of the English line
  (``caches.get_voice_translation``), so tapping the same line again never
  pays twice. Bump ``caches.VOICE_TRANSLATION_VERSION`` with the prompt.
- Every failure falls back to English speech: he always hears her.
"""

from __future__ import annotations

import asyncio
import logging
import re

from . import caches
from .db import get_conn, get_conn_autocommit
from .helpers import strip_actions_for_tts, tts_clip

logger = logging.getLogger(__name__)

VOICE_LANGUAGES = ("en", "ja")
DEFAULT_LANGUAGE = "en"

TRANSLATION_TIMEOUT = 20.0
_MAX_SPEECH_CHARS = 500

TRANSLATION_PROMPT = """\
You are the Japanese voice of Klukai, the T-Doll from Girls' Frontline 2: \
Exilium. Translate her English line into the natural spoken Japanese she \
would say aloud to the Commander.

Her register:
- Polite です/ます form with the Commander, always.
- Elegant, calm and confident: composed, precise, quietly warm; never \
gushing, never stiff.
- The Commander is 指揮官. Her own name is クルカイ.
- Always write "404" as ヨンマルヨン.

Output rules:
- Reply with the Japanese line only: no English, no romaji, no quotation \
marks, no notes or explanations.
- Leave out stage directions and actions such as (she smiles) or *nods*; \
translate only what she says aloud.
- Keep the meaning and the feeling; add nothing and drop nothing."""

_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")
_THINK = re.compile(r"<\|?think\|?>.*?<\|?/think\|?>", re.DOTALL)
_FENCE = re.compile(r"```[a-zA-Z]*")
_FULLWIDTH_DIRECTIONS = re.compile(r"（[^）]*）|＊[^＊]*＊")
_SQUAD_404 = re.compile(
    r"(?<![0-9\uff10-\uff19])(?:404|\uff14\uff10\uff14)(?![0-9\uff10-\uff19])"
)
_QUOTES = "\"'“”「」『』"


def normalize_language(value: object) -> str:
    if isinstance(value, str) and value in VOICE_LANGUAGES:
        return value
    return DEFAULT_LANGUAGE


# ── The per-Commander setting ─────────────────────────────────────────────


async def load_voice_language(user_id: str) -> str:
    """The stored choice; ``"en"`` without a row. Raises on a database error."""
    async with get_conn() as conn:
        row = await (
            await conn.execute(
                "SELECT voice_language FROM companion_user_settings WHERE user_id = %s",
                (user_id,),
            )
        ).fetchone()
    return normalize_language(row[0] if row else None)


async def voice_language_for(user_id: str) -> str:
    """The choice for speech. A database outage speaks English, never fails."""
    try:
        return await load_voice_language(user_id)
    except Exception as exc:
        logger.warning("Voice language unavailable, using English: %s", exc)
        return DEFAULT_LANGUAGE


async def save_voice_language(user_id: str, language: str) -> str:
    if language not in VOICE_LANGUAGES:
        raise ValueError(f"Unsupported voice language: {language!r}")
    async with get_conn_autocommit() as conn:
        await conn.execute(
            "INSERT INTO companion_user_settings (user_id, voice_language, updated_at) "
            "VALUES (%s, %s, NOW()) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "voice_language = EXCLUDED.voice_language, updated_at = NOW()",
            (user_id, language),
        )
    return language


# ── Translation ───────────────────────────────────────────────────────────


def _clean(raw: str) -> str | None:
    """Her spoken Japanese, or None when the model did not give us any."""
    text = _THINK.sub("", raw)
    text = _FENCE.sub("", text)
    text = _FULLWIDTH_DIRECTIONS.sub("", text)
    text = strip_actions_for_tts(text).strip(_QUOTES + " ")
    # Deterministic, whatever the model wrote: her squad is read ヨンマルヨン.
    text = _SQUAD_404.sub("ヨンマルヨン", text)
    if not text or not _JAPANESE.search(text):
        return None
    return tts_clip(text, _MAX_SPEECH_CHARS)


async def _ask_llm(text: str) -> str | None:
    from .context import router
    from .llm_router import FAILURE_SENTINEL
    from .models import SessionState

    session = SessionState(conversation_id="voice-translation")
    config = await router.route(text, session, interactive=True)
    parts: list[str] = []
    async for token in router.stream(
        TRANSLATION_PROMPT, [{"role": "user", "content": text}], config
    ):
        parts.append(token)
    raw = "".join(parts)
    if not raw or FAILURE_SENTINEL in raw:
        return None
    return _clean(raw)


async def translate_to_japanese(text: str) -> str | None:
    """Her line in Japanese, cached; None on any failure."""
    text = text.strip()
    if not text:
        return None
    cached = await caches.get_voice_translation(text)
    if cached:
        return cached
    try:
        async with asyncio.timeout(TRANSLATION_TIMEOUT):
            japanese = await _ask_llm(text)
    except Exception as exc:
        logger.warning("Voice translation failed: %s", type(exc).__name__)
        return None
    if japanese is None:
        logger.warning("Voice translation returned no Japanese line")
        return None
    await caches.put_voice_translation(text, japanese)
    return japanese


async def speech_for(text: str, language: str) -> tuple[str, str]:
    """``(text, language)`` to synthesize. Japanese falls back to English."""
    if language != "ja":
        return text, DEFAULT_LANGUAGE
    japanese = await translate_to_japanese(text)
    if japanese is None:
        return text, DEFAULT_LANGUAGE
    return japanese, "ja"


__all__ = [
    "DEFAULT_LANGUAGE",
    "TRANSLATION_PROMPT",
    "VOICE_LANGUAGES",
    "load_voice_language",
    "save_voice_language",
    "speech_for",
    "translate_to_japanese",
    "voice_language_for",
]
