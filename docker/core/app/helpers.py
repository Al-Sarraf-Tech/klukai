"""Pure helper functions: narration fixing, image prompts, text processing.

No I/O, no state, no imports from other app modules. The intent / exit-cue
detectors live in ``helpers_intent`` and are re-exported below.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

from .image_gen import SQUAD_KEYWORDS, SITUATION_KEYWORDS

# Intent / exit-cue detection lives in helpers_intent (split for the 500-line
# gate). Every name is re-exported so `from .helpers import ...` and
# `patch("app.helpers.<name>")` keep working: chat_prompt imports the detectors
# inside its functions, so a patch here is what those call sites resolve.
from .helpers_intent import (  # noqa: F401
    # keyword lists + squad / trivial tables
    RECALL_KEYWORDS, SAVE_KEYWORDS, DISCARD_KEYWORDS,
    MISSION_START_KEYWORDS, MISSION_CANCEL_KEYWORDS,
    SQUAD_MEMBERS, SQUAD_ADDRESS_PATTERNS, TRIVIAL_PATTERNS,
    JEALOUSY_COMPLIMENT_PATTERNS, JEALOUSY_SQUAD_NAMES,
    COMMANDER_DETAIL_CATEGORIES, DREAM_INQUIRY_KEYWORDS,
    THREAD_EXPLICIT_PHRASES, THREAD_DECADE_ANCHORS, _THREAD_MESSAGE_WORD,
    # detectors
    detect_squad_address, detect_jealousy_trigger,
    detect_commander_details, detect_gift_giving,
    wants_dream_inquiry, wants_thread_inquiry, wants_recall,
    wants_mission_start, wants_mission_cancel, parse_interval_minutes,
    # clean goodbyes: exit-cue detection and its clause machinery
    GoodbyeKind, _GOODBYE_MAX_LEN, _GOODBYE_TAIL_CHARS,
    _CLAUSE, _WORD, _END, _LEAVE_VERB, _WORKPLACE, _NOT_A_GOODNIGHT,
    _NIGHT_PHRASES, _LEAVE_PHRASES, _NIGHT_ALONE, _LEAVE_ALONE, _ALONE_STRIP,
    _NEGATIONS, _OTHER_SUBJECTS, _WISHES, _PAST_ARTICLE,
    _goodbye_blocked, _next_clause_has_substance, _clause_kind, detect_goodbye,
)

if TYPE_CHECKING:
    from fastapi import Request


def voice_auth_headers() -> dict[str, str]:
    """Bearer header for calls to the voice service when VOICE_API_TOKEN is set.

    Empty (no auth) when unset, so it stays a no-op until the token is
    provisioned on the voice host — core->voice keeps working either way.
    """
    tok = os.environ.get("VOICE_API_TOKEN")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def client_ip(request: "Request") -> str:
    """Best-effort real client IP behind Cloudflare → cloudflared → loopback.

    The socket peer (request.client.host) is always the tunnel's internal IP, so
    a naive per-IP ban or audit would treat every external user as one host —
    locking out the owner and recording useless forensics. Prefer Cloudflare's
    CF-Connecting-IP, then the first X-Forwarded-For hop, then the peer.
    """
    cf = request.headers.get("cf-connecting-ip")
    if cf and cf.strip():
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff and xff.strip():
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def chunk_text(text: str, chunk_size: int = 8) -> list[str]:
    """Split text into chunks for simulated streaming."""
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def fix_narration(text: str) -> str:
    """Fix second-person narration and clean up model artifacts."""
    # Strip R1 reasoning blocks: <think>...</think>
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'<\|think\|>.*?<\|/think\|>', '', text, flags=re.DOTALL)
    # Convert "(You verb...)" to "(I verb...)". Match one-or-more spaces so a
    # later double-space collapse can't leave a "(You  verb)" that only converts
    # on a second pass — keeps fix_narration idempotent.
    text = re.sub(r'\(You +([a-z])', lambda m: f'(I {m.group(1)}', text)
    # Convert "(Your noun)" to "(My noun)"
    text = re.sub(r'\(Your +', '(My ', text)
    text = re.sub(r'\(your +', '(my ', text)
    # Strip parentheticals that narrate Commander's actions/appearance
    text = re.sub(
        r'\([^)]*(?:your face|your eyes|your expression|your mouth|crosses your|touches your)[^)]*\)',
        '', text,
    )
    # Strip trailing pipe characters (dolphin-glm reasoning artifact) together
    # with any surrounding spaces, looping so a "| " tail (pipe then space)
    # collapses fully in a single call. Stripping spaces first keeps this
    # idempotent — see tests/property/test_parsers_properties.py.
    text = text.rstrip(' ')
    while text.endswith('|'):
        text = text[:-1].rstrip(' ')
    # Clean up double spaces from removals
    text = re.sub(r'  +', ' ', text)
    return text


def enhance_image_prompt(user_request: str, couple: bool = False) -> str:
    """Fast keyword-based tag generation — no LLM call needed."""
    lower = user_request.lower()
    tags = []

    SCENE_MAP = {
        "sunset": "sunset, orange sky, golden hour lighting",
        "night": "night, moonlight, dark sky, stars",
        "rain": "rain, wet, umbrella, overcast",
        "snow": "snow, winter, cold breath, scarf",
        "beach": "beach, ocean, sand, swimsuit, summer",
        "cafe": "cafe, table, coffee cup, indoor, cozy",
        "battle": "battlefield, smoke, debris, action pose",
        "motorcycle": "motorcycle, riding, wind, speed lines, road",
        "bed": "bedroom, bed, pillows, soft lighting, intimate",
        "rooftop": "rooftop, city skyline, wind, evening",
        "garden": "garden, flowers, natural lighting, peaceful",
        "office": "office, desk, computer, indoor lighting",
        "forest": "forest, trees, nature, sunlight through leaves",
        "city": "city, urban, street, buildings, neon",
    }
    for keyword, scene_tags in SCENE_MAP.items():
        if keyword in lower:
            tags.append(scene_tags)

    MOOD_MAP = {
        "kiss": "kiss, eyes closed, romantic",
        "hug": "hug, embrace, close, warm",
        "cuddle": "cuddling, lying down, comfortable, close",
        "hold": "holding hands, close, side by side",
        "smile": "smile, happy, cheerful",
        "blush": "blush, embarrassed, looking away",
        "cry": "tears, emotional, sad",
        "fight": "fighting stance, action, dynamic pose",
        "sleep": "sleeping, peaceful, eyes closed",
        "eat": "eating, food, table",
        "cook": "cooking, kitchen, apron",
        "read": "reading, book, sitting, quiet",
    }
    for keyword, mood_tags in MOOD_MAP.items():
        if keyword in lower:
            tags.append(mood_tags)

    for keyword, sit_tags in SITUATION_KEYWORDS.items():
        if keyword in lower:
            tags.append(sit_tags)

    for member, member_tags in SQUAD_KEYWORDS.items():
        if member in lower:
            tags.append(member_tags)
            tags.append("multiple girls" if not couple else "")

    if not tags:
        tags.append("standing, looking at viewer, detailed background")

    return ", ".join(t for t in tags if t)


def strip_actions_for_tts(text: str) -> str:
    """Remove stage directions — "(she smiles)" and "*nods*" — for natural voice output."""
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\*[^*]*\*', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# Sentence enders tts_clip may cut after. The trailing space/newline keeps a
# decimal point ("2.5") or an abbreviation mid-sentence from counting.
_TTS_BOUNDARIES = (". ", "! ", "? ", ".\n")


def tts_clip(text: str, limit: int = 500) -> str:
    """Cap text for XTTS at ``limit`` chars, cutting at the last sentence end.

    XTTS rejects very long inputs, and a hard cut leaves her trailing off
    mid-word. When the text is over the limit, cut after the last sentence
    boundary inside it; fall back to a hard cut only when that boundary is so
    early (before ``limit // 3``) that it would drop most of the clip.
    """
    if len(text) <= limit:
        return text
    # Search one char past the limit so a sentence ending exactly at the
    # limit (its space falling just outside) still counts.
    window = text[: limit + 1]
    cut = max(window.rfind(b) for b in _TTS_BOUNDARIES)
    if cut < limit // 3:
        return text[:limit]
    return text[: cut + 1]


# ── DB helpers ───────────────────────────────────────────────────────────────

async def create_conversation(conv_id: str, user_id: str = "jalsarraf") -> None:
    """Create a new conversation record scoped to a user."""
    import logging
    from .db import get_conn_autocommit
    logger = logging.getLogger(__name__)
    try:
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "INSERT INTO companion_conversations (id, user_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (conv_id, user_id),
            )
    except Exception as e:
        logger.error("Failed to create conversation: %s", e)


async def store_message(
    conversation_id: str,
    role: str,
    content: str,
    model: str = "",
    latency_ms: int | None = None,
    user_id: str = "jalsarraf",
) -> bool:
    """Store a message and update conversation turn count atomically.

    Returns True when the message was persisted, False on DB failure so the
    chat path can warn the Commander instead of silently losing the message.
    """
    import logging
    from .db import get_conn
    logger = logging.getLogger(__name__)
    try:
        async with get_conn() as conn:
            await conn.execute(
                "INSERT INTO companion_messages "
                "(conversation_id, role, content, model, latency_ms, user_id) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (conversation_id, role, content, model, latency_ms, user_id),
            )
            await conn.execute(
                "UPDATE companion_conversations SET turn_count = turn_count + 1, "
                "model_used = %s WHERE id = %s",
                (model, conversation_id),
            )
            await conn.commit()
        return True
    except Exception as e:
        logger.error("Failed to store message: %s", e)
        return False
