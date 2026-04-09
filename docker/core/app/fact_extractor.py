"""Background fact extraction from conversations."""

from __future__ import annotations

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Background tasks use LM Studio (gemma-4-e2b-it on Intel Arc, delayed 3s after chat)
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://192.168.50.2:1234")
EXTRACTION_MODEL = "gemma-4-e2b-it"

# Shared httpx client — initialized on first use, reused thereafter
_http: httpx.AsyncClient | None = None


def _get_http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=30.0)
    return _http

FACT_EXTRACTION_PROMPT = """\
You are Klukai's internal memory processor. Extract information about the Commander
from this exchange. Focus on what a devoted elite T-Doll squad leader would consider
operationally important:
- Tactical preferences (how they approach problems, decision-making style)
- Personal details they've shared (work, hobbies, interests, daily life)
- Emotional state patterns (stress, energy, morale)
- Promises made (by either party)
- Things that pleased or displeased the Commander
- Items, gifts, or resources mentioned

Return a JSON object with:
- "facts": list of {{"key": "short_key", "value": "fact description"}} — only NEW information about the Commander
- "mood": Klukai's emotional state after this exchange. Choose the MOST fitting one:
  Core: composed, focused, prideful, exasperated, protective, quietly_pleased, competitive, tender, longing, battle_ready
  Romantic: flustered, affectionate, shy, yearning, devoted
  Tactical: vigilant, calculating, hunting, adrenaline
  Relaxed: content, playful, drowsy, amused, bored
  Dark: melancholic, haunted, conflicted, guilty, determined
  Other: nostalgic, curious, irritated, defiant, vulnerable
- "topics": list of discussion topics mentioned
- "should_remember": boolean — true if this exchange contains something worth preserving in long-term operational records

If no new facts, return empty lists. Return ONLY valid JSON, no other text.

Exchange:
Commander: {user_message}
Klukai: {assistant_message}
"""

IMAGE_CURATION_ADDENDUM = """
An image was generated during this exchange. Evaluate it for Klukai's memory archive.
IMPORTANT: The Commander is HUMAN (male). He is NOT a T-Doll. Never describe him as one.

- "keep": true/false — would Klukai consider this moment worth preserving?
- "annotation": 1-2 sentence caption as Klukai (first person, in character). Write like a private journal, not a report.
- "category": one of: {categories}
- "image_tags": list of scene/setting keywords for search. If the Commander is present, include "couple" and "1boy".

Add a "memory_curation" key to your JSON response with these fields.
"""


async def extract_facts(
    user_message: str,
    assistant_message: str,
    image_generated: bool = False,
    affection_level: int = 0,
) -> dict:
    """Extract facts, mood, and topics from a conversation exchange.

    Args:
        user_message: The user's message text.
        assistant_message: Klukai's response text.
        image_generated: If True, append curation prompt and extract memory_curation.
        affection_level: Current affection level, used to determine valid categories.
    """
    from .memory_archive import available_categories

    prompt = FACT_EXTRACTION_PROMPT.format(
        user_message=user_message[:1000],
        assistant_message=assistant_message[:1000],
    )

    if image_generated:
        categories = ", ".join(available_categories(affection_level))
        prompt += IMAGE_CURATION_ADDENDUM.format(categories=categories)

    try:
        from .llm_router import get_lm_gate

        gate = get_lm_gate()
        async with gate:  # Waits for main chat to finish streaming
            client = _get_http()
            r = await client.post(
                f"{LM_STUDIO_URL}/v1/chat/completions",
                json={
                    "model": EXTRACTION_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 512,
                    "temperature": 0.1,
                    "stream": False,
                },
            )
            r.raise_for_status()
            data = r.json()
            choices = data.get("choices", [])
            if not choices:
                logger.warning("Extraction LLM returned empty choices: %s", data)
                return {"facts": [], "mood": "composed", "topics": [], "should_remember": False}
            content = choices[0].get("message", {}).get("content") or ""
            if not content.strip():
                logger.warning("Extraction LLM returned empty content")
                return {"facts": [], "mood": "composed", "topics": [], "should_remember": False}

        # Parse JSON from response (handle markdown code blocks + R1 think tags)
        import re
        content = content.strip()
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1]
            content = content.rsplit("```", 1)[0]

        result = json.loads(content)

        # Validate mood is in the known set
        VALID_MOODS = {
            "composed", "focused", "prideful", "exasperated", "protective",
            "quietly_pleased", "competitive", "tender", "longing", "battle_ready",
            "flustered", "affectionate", "shy", "yearning", "devoted",
            "vigilant", "calculating", "hunting", "adrenaline",
            "content", "playful", "drowsy", "amused", "bored",
            "melancholic", "haunted", "conflicted", "guilty", "determined",
            "nostalgic", "curious", "irritated", "defiant", "vulnerable",
        }
        mood = result.get("mood", "composed")
        if mood not in VALID_MOODS:
            logger.warning("Invalid mood '%s' from extraction, defaulting to composed", mood)
            mood = "composed"

        out: dict = {
            "facts": result.get("facts", []),
            "mood": mood,
            "topics": result.get("topics", []),
            "should_remember": result.get("should_remember", False),
        }

        if image_generated and "memory_curation" in result:
            out["memory_curation"] = result["memory_curation"]

        return out
    except Exception as e:
        logger.warning("Fact extraction failed (%s): %s", type(e).__name__, e)
        return {
            "facts": [],
            "mood": "composed",
            "topics": [],
            "should_remember": False,
        }


async def create_episode_summary(
    turns: list[dict], max_turns: int = 10
) -> str | None:
    """Summarize a conversation segment for episodic memory."""
    if len(turns) < 3:
        return None

    recent = turns[-max_turns:]
    conversation = "\n".join(
        f"{t['role'].title()}: {t['content'][:200]}" for t in recent
    )

    prompt = (
        "You are Klukai, writing a brief operational log entry about your interaction "
        "with the Commander. Summarize in 1-2 sentences using first person. Note anything "
        "significant about the Commander's behavior, mood, or shared information. "
        "Be concise and professional, but let your investment in the Commander show subtly.\n\n"
        f"{conversation}"
    )

    try:
        from .llm_router import get_lm_gate

        gate = get_lm_gate()
        async with gate:  # Waits for main chat to finish streaming
            client = _get_http()
            r = await client.post(
                f"{LM_STUDIO_URL}/v1/chat/completions",
                json={
                    "model": EXTRACTION_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 150,
                    "temperature": 0.3,
                    "stream": False,
                },
            )
            r.raise_for_status()
            data = r.json()
            choices = data.get("choices", [])
            if not choices:
                logger.warning("Episode summary LLM returned empty choices")
                return None
            return (choices[0].get("message", {}).get("content") or "").strip() or None
    except Exception as e:
        logger.warning("Episode summary failed (%s): %s", type(e).__name__, e)
        return None
