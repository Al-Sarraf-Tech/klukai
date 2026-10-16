"""Real exchange selection and validated first-person scene composition."""

from __future__ import annotations

import logging
import random
import re
from typing import Any, TypedDict

logger = logging.getLogger(__name__)


class POVScene(TypedDict):
    annotation: str
    scene_tags: str
    couple: bool
    mood: str
    title: str


_TRIVIAL = {
    "ok",
    "okay",
    "yes",
    "no",
    "yeah",
    "yep",
    "nope",
    "sure",
    "thanks",
    "thank you",
    "hi",
    "hey",
    "hello",
    "good",
    "nice",
    "cool",
    "hm",
    "hmm",
}


def _is_trivial(text: str) -> bool:
    t = (text or "").strip().lower()
    if len(t) < 12:
        return True
    return t in _TRIVIAL


async def pick_exchange(user_id: str) -> dict[str, Any] | None:
    """Pick a real user↔assistant exchange from chat history.

    Prefers substantive turns (length + non-proactive assistant rows). Pure
    selection — no LLM call. Returns None if history is too thin.
    """
    from .db import get_conn

    async with get_conn() as conn:
        rows = await (
            await conn.execute(
                "SELECT id, role, content, mood, model, created_at "
                "FROM companion_messages "
                "WHERE user_id = %s AND role IN ('user', 'assistant') "
                "AND COALESCE(model, '') <> 'proactive' "
                "ORDER BY created_at DESC LIMIT 120",
                (user_id,),
            )
        ).fetchall()

    if not rows:
        return None

    # Chronological for pairing
    rows = list(reversed(rows))
    candidates: list[dict[str, Any]] = []
    pending_user: dict[str, Any] | None = None
    for row in rows:
        rid, role, content, mood, model, created = row
        content = (content or "").strip()
        if role == "user":
            pending_user = {
                "user_id_msg": str(rid),
                "user_content": content,
                "created_at": created.isoformat()
                if hasattr(created, "isoformat")
                else str(created),
            }
            continue
        if role == "assistant" and pending_user and content:
            if _is_trivial(pending_user["user_content"]) and _is_trivial(content):
                pending_user = None
                continue
            score = min(len(pending_user["user_content"]), 400) + min(len(content), 600)
            # Slight boost for longer commander lines (more "moment" material)
            if len(pending_user["user_content"]) >= 40:
                score += 80
            candidates.append(
                {
                    **pending_user,
                    "assistant_content": content[:1200],
                    "user_content": pending_user["user_content"][:800],
                    "mood": mood or "composed",
                    "score": score,
                }
            )
            pending_user = None

    if not candidates:
        return None

    # Weighted random among top half by score
    candidates.sort(key=lambda c: c["score"], reverse=True)
    pool = candidates[: max(3, len(candidates) // 2)]
    weights = [max(1, c["score"]) for c in pool]
    return random.choices(pool, weights=weights, k=1)[0]


_POV_PROMPT = """\
You are Klukai (H.I.D.E. 404 leader). The Commander asked you to pick a real \
moment from your shared history and draw it from YOUR point of view.

Exchange (verbatim):
Commander: {user_msg}
You: {assistant_msg}

Write JSON ONLY (no markdown):
{{
  "annotation": "1-2 sentences. First person. Your private journal voice. \
Denial/understatement OK. Never generic. Never say you are an AI.",
  "scene_tags": "danbooru-style tags for the VISUAL from your POV or of you in \
that moment (setting, action, expression, lighting). Comma-separated. No nsfw.",
  "couple": true/false,
  "mood": "one mood word (tender, composed, longing, playful, etc.)",
  "title": "2-5 word title for the menu card"
}}

Affection level: {affection_level}/9. Higher = more open warmth in the journal line.
couple=true only if the Commander is visually present in the scene with you.
"""


def _validate_cloud_scene(raw: object) -> dict[str, Any]:
    from .creative_text import CreativeTextError

    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("annotation"), str)
        or len(raw["annotation"].strip()) < 8
        or not isinstance(raw.get("scene_tags"), str)
        or len(raw["scene_tags"].strip()) < 8
        or not isinstance(raw.get("couple"), bool)
        or not isinstance(raw.get("mood"), str)
        or not raw["mood"].strip()
        or not isinstance(raw.get("title"), str)
        or not raw["title"].strip()
    ):
        raise CreativeTextError("Malformed creative memory fields")
    return raw


async def compose_pov(
    exchange: dict[str, Any],
    affection_level: int,
) -> POVScene:
    """LLM: journal annotation + scene tags for the picked exchange."""
    from .llm_json import call_llm
    import os

    LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://100.107.121.5:1234")

    prompt = _POV_PROMPT.format(
        user_msg=exchange.get("user_content", "")[:700],
        assistant_msg=exchange.get("assistant_content", "")[:900],
        affection_level=affection_level,
    )
    model = "cognitivecomputations_dolphin-mistral-24b-venice-edition"
    from .llm_router import get_lm_gate
    from .creative_text import (
        CreativeTextError,
        complete_creative_text,
        uses_cloud_text,
    )

    raw: dict[str, Any] = {}
    if uses_cloud_text():
        import json

        content = await complete_creative_text(
            "Return only the requested memory JSON.", prompt
        )
        try:
            raw = json.loads(content)
        except (TypeError, ValueError):
            raise CreativeTextError("Malformed creative memory JSON") from None
        raw = _validate_cloud_scene(raw)
    else:
        try:
            async with get_lm_gate():
                raw = await call_llm(
                    LM_STUDIO_URL,
                    model,
                    prompt,
                    max_tokens=512,
                    temperature=0.4,
                )
        except Exception as e:
            logger.warning("compose_pov LLM failed: %s", e)
            raw = {}

    if not isinstance(raw, dict):
        raw = {}

    annotation = raw.get("annotation")
    if not isinstance(annotation, str) or len(annotation.strip()) < 8:
        annotation = "...I pulled this from our records. Don't read into why I kept it."
    scene_tags = raw.get("scene_tags")
    if not isinstance(scene_tags, str) or len(scene_tags.strip()) < 8:
        scene_tags = (
            "indoors, soft lighting, silver hair, high ponytail, "
            "tactical outfit, looking at viewer, serious expression"
        )
    couple = bool(raw.get("couple")) if isinstance(raw.get("couple"), bool) else False
    raw_mood = raw.get("mood")
    mood = (
        raw_mood
        if isinstance(raw_mood, str)
        else str(exchange.get("mood") or "composed")
    )
    raw_title = raw.get("title")
    title = raw_title if isinstance(raw_title, str) else "From my side"
    title = re.sub(r"\s+", " ", title).strip()[:48] or "From my side"

    return {
        "annotation": annotation.strip()[:500],
        "scene_tags": scene_tags.strip()[:600],
        "couple": couple,
        "mood": mood.strip()[:40] or "composed",
        "title": title,
    }
