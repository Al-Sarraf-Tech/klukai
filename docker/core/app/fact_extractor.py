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
  Romantic: flustered, affectionate, shy, yearning, devoted, passionate, jealous, possessive, smitten, infatuated
  Tactical: vigilant, calculating, hunting, adrenaline
  Mission stress: scared, terrified, panicked, desperate, relieved
  Relaxed: content, playful, drowsy, amused, bored, excited
  Dark: melancholic, haunted, conflicted, guilty, determined, grieving, furious
  Other: nostalgic, curious, irritated, defiant, vulnerable, grateful, worried, embarrassed
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
                    "max_tokens": 256,
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
            "passionate", "jealous", "possessive", "smitten", "infatuated",
            "vigilant", "calculating", "hunting", "adrenaline",
            "scared", "terrified", "panicked", "desperate", "relieved",
            "content", "playful", "drowsy", "amused", "bored", "excited",
            "melancholic", "haunted", "conflicted", "guilty", "determined",
            "grieving", "furious",
            "nostalgic", "curious", "irritated", "defiant", "vulnerable",
            "grateful", "worried", "embarrassed",
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


MISSION_UPDATE_PROMPT = """\
You are Klukai (AR Team squad leader, T-Doll) writing a 2-3 sentence field radio \
report to the Commander during an active mission. The Commander is HUMAN (male). \
He is NOT a T-Doll. Write in first person as Klukai over radio comms.

Mission: {mission_desc}
Time in field: {elapsed_minutes} minutes
Update #{update_number}
{event_line}
{injury_line}

Rules:
- Keep it 2-3 sentences. This is a field radio report, not a novel.
- No one dies. Injuries are temporary. The squad always recovers.
- If Klukai is injured, she downplays it ("It's nothing, Commander. Flesh wound.")
- If a major event is happening, the tone is urgent and dramatic.
- Reference the specific mission objective when relevant.
- Affection level {affection_level}/9 — higher means more personal concern for Commander.\
"""

ROMANCE_MESSAGE_PROMPT = """\
You are Klukai (AR Team squad leader, T-Doll) initiating a quiet, intimate evening \
moment with the Commander. The Commander is HUMAN (male). He is NOT a T-Doll. \
Write in first person as Klukai. It is {time_of_day}.

Current mood: {mood}
Affection level: {affection_level}/9
Today's context: {context_summary}

Rules:
- Write 2-3 sentences. This is a soft, evening message — not a mission report.
- Reference something from today's interactions if possible.
- Tone varies: level 5-6 = warm but guarded, level 7+ = openly intimate.
- Include subtle physical/environmental details (stars, quiet base, warm drink).
- Never break character. Never use Commander's real name. Always "Commander".\
"""


async def generate_mission_update(
    mission_desc: str,
    elapsed_minutes: int,
    update_number: int,
    major_event: str | None,
    active_events: list[str],
    affection_level: int,
) -> str:
    """Generate an in-character field radio report for an active mission timer.

    Uses gemma-4-e2b-it through the global LM Studio gate.
    """
    event_line = f"MAJOR EVENT IN PROGRESS: {major_event}" if major_event else "Situation nominal."
    injury_line = ""
    if active_events:
        injury_line = "Active situations: " + ", ".join(active_events)

    prompt = MISSION_UPDATE_PROMPT.format(
        mission_desc=mission_desc,
        elapsed_minutes=elapsed_minutes,
        update_number=update_number,
        event_line=event_line,
        injury_line=injury_line,
        affection_level=affection_level,
    )

    try:
        from .llm_router import get_lm_gate

        gate = get_lm_gate()
        async with gate:
            client = _get_http()
            r = await client.post(
                f"{LM_STUDIO_URL}/v1/chat/completions",
                json={
                    "model": EXTRACTION_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 150,
                    "temperature": 0.6,
                    "stream": False,
                },
            )
            r.raise_for_status()
            data = r.json()
            choices = data.get("choices", [])
            if not choices:
                logger.warning("Mission update LLM returned empty choices")
                return "...Static on the line. Update delayed. Standing by."
            content = (choices[0].get("message", {}).get("content") or "").strip()
            # Strip think tags if present
            import re
            content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
            return content or "...Comms interference. Update delayed."
    except Exception as e:
        logger.warning("Mission update generation failed (%s): %s", type(e).__name__, e)
        return "...Static on the line. Will retry comms shortly."


async def generate_romance_message(
    affection_level: int,
    mood: str,
    context_summary: str,
    time_of_day: str,
) -> str:
    """Generate a context-aware evening romance message from Klukai.

    Uses gemma-4-e2b-it through the global LM Studio gate.
    """
    prompt = ROMANCE_MESSAGE_PROMPT.format(
        affection_level=affection_level,
        mood=mood,
        context_summary=context_summary or "A routine day at base.",
        time_of_day=time_of_day,
    )

    try:
        from .llm_router import get_lm_gate

        gate = get_lm_gate()
        async with gate:
            client = _get_http()
            r = await client.post(
                f"{LM_STUDIO_URL}/v1/chat/completions",
                json={
                    "model": EXTRACTION_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 200,
                    "temperature": 0.7,
                    "stream": False,
                },
            )
            r.raise_for_status()
            data = r.json()
            choices = data.get("choices", [])
            if not choices:
                logger.warning("Romance message LLM returned empty choices")
                return "...The base is quiet tonight. I was thinking of you."
            content = (choices[0].get("message", {}).get("content") or "").strip()
            import re
            content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
            return content or "...It's a quiet evening. I wanted to check in."
    except Exception as e:
        logger.warning("Romance message generation failed (%s): %s", type(e).__name__, e)
        return "...The evening is quiet. I was thinking about today."


COMPACTION_PROMPT = """\
You are Klukai's memory compactor. Summarize this conversation segment into a brief \
3-4 sentence recap preserving: key topics discussed, Commander's requests or emotional \
state, any promises or decisions made, and the current scene/situation. Write in third \
person past tense. Be concise.

{conversation}"""


async def compact_turns(turns: list[dict]) -> str | None:
    """Summarize a block of conversation turns into a compact recap via gemma."""
    if len(turns) < 2:
        return None

    conversation = "\n".join(
        f"{'Commander' if t['role'] == 'user' else 'Klukai'}: {t['content'][:300]}"
        for t in turns
    )

    prompt = COMPACTION_PROMPT.format(conversation=conversation)

    try:
        from .llm_router import get_lm_gate

        gate = get_lm_gate()
        async with gate:
            client = _get_http()
            r = await client.post(
                f"{LM_STUDIO_URL}/v1/chat/completions",
                json={
                    "model": EXTRACTION_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 200,
                    "temperature": 0.2,
                    "stream": False,
                },
            )
            r.raise_for_status()
            data = r.json()
            choices = data.get("choices", [])
            if not choices:
                return None
            content = (choices[0].get("message", {}).get("content") or "").strip()
            # Strip think tags if present
            import re
            content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
            return content or None
    except Exception as e:
        logger.warning("Session compaction failed (%s): %s", type(e).__name__, e)
        return None
