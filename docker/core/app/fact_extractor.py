"""Background fact extraction from conversations."""

from __future__ import annotations

import json
import logging
import os
import uuid

import httpx

logger = logging.getLogger(__name__)

LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://192.168.50.2:1234")
EXTRACTION_MODEL = "qwen2.5-3b-instruct"

FACT_EXTRACTION_PROMPT = """\
Extract facts about the user from this conversation exchange. Return a JSON object with:
- "facts": list of {"key": "short_key", "value": "fact description"} (e.g., {"key": "favorite_language", "value": "Rust"})
- "mood": the user's apparent mood (one of: neutral, happy, curious, concerned, playful, thoughtful, excited, tired, annoyed)
- "topics": list of discussion topics mentioned
- "should_remember": boolean - true if this exchange contains something worth remembering long-term

Only include facts that are NEW information about the user. If no new facts, return empty lists.
Return ONLY valid JSON, no other text.

Exchange:
User: {user_message}
Assistant: {assistant_message}
"""


async def extract_facts(
    user_message: str, assistant_message: str
) -> dict:
    """Extract facts, mood, and topics from a conversation exchange."""
    prompt = FACT_EXTRACTION_PROMPT.format(
        user_message=user_message[:1000],
        assistant_message=assistant_message[:1000],
    )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
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
            content = r.json()["choices"][0]["message"]["content"]

            # Parse JSON from response (handle markdown code blocks)
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1]
                content = content.rsplit("```", 1)[0]

            result = json.loads(content)
            return {
                "facts": result.get("facts", []),
                "mood": result.get("mood", "neutral"),
                "topics": result.get("topics", []),
                "should_remember": result.get("should_remember", False),
            }
    except Exception as e:
        logger.warning("Fact extraction failed: %s", e)
        return {
            "facts": [],
            "mood": "neutral",
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
        "Summarize this conversation in 1-2 sentences. Focus on the key topics "
        "discussed and any important decisions or facts shared. Be concise.\n\n"
        f"{conversation}"
    )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
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
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning("Episode summary failed: %s", e)
        return None
