"""Background fact extraction, mood classification, and content generation.

All LLM calls go through llm_json.call_llm() or call_llm_text() —
rock-solid JSON parsing with automatic reasoning field extraction.
"""

from __future__ import annotations

import logging
import os

from .llm_json import call_llm, call_llm_text

logger = logging.getLogger(__name__)

LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://100.107.121.5:1234")
EXTRACTION_MODEL = "cognitivecomputations_dolphin-mistral-24b-venice-edition"

# ── Valid moods (must match models.py Mood enum) ─────────────────────────

VALID_MOODS = frozenset({
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
})

_DEFAULT_RESULT: dict = {
    "facts": [],
    "mood": "composed",
    "topics": [],
    "should_remember": False,
    "interaction": {"type": "neutral", "intensity": 5},
    "commander_details": {},
    "gift_item": None,
    "inside_joke": None,
}

# ── Prompts ──────────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """\
Analyze this exchange. The Commander is HUMAN (male). Klukai is a T-Doll.
Klukai's current affection: level {affection_level}/9. At high levels (7+), romantic \
and tender moods are natural responses to warmth. At low levels (0-2), composed or \
prideful moods dominate.

Return ONLY valid JSON with these fields:
{{"mood":"<one word from the list>","interaction":{{"type":"<type>","intensity":<1-10>}},"facts":[],"topics":[],"should_remember":false,"commander_details":{{}},"gift_item":null,"inside_joke":null}}

Moods: composed, focused, prideful, exasperated, protective, quietly_pleased, \
competitive, tender, longing, battle_ready, flustered, affectionate, shy, yearning, \
devoted, passionate, jealous, possessive, smitten, infatuated, vigilant, calculating, \
hunting, adrenaline, scared, terrified, panicked, desperate, relieved, content, playful, \
drowsy, amused, bored, excited, melancholic, haunted, conflicted, guilty, determined, \
grieving, furious, nostalgic, curious, irritated, defiant, vulnerable, grateful, worried, embarrassed

Interaction types: greeting, genuine_interest, personal_sharing, compliment, \
mission_discussion, remembering, neutral, flirty, playful, tender, warm, affectionate, \
combative, hostile, angry, rude, sad, hurt, distressed, vulnerable
IMPORTANT: Short/casual messages are NEVER "rude". Only explicit hostility is "rude".
"I love you" is a compliment (intensity 8-10) OR tender/warm (intensity 8-10), NOT neutral.
IMPORTANT: If the Commander says he is struggling, in pain, grieving, exhausted, \
lonely, scared, or "not okay" — even briefly, flatly, or while insisting he does not \
want to talk about it — classify it as "sad", "hurt", "distressed" or "vulnerable". \
Never "neutral", and never "genuine_interest". Understatement is how he reports pain; \
read the content, not the tone. Bereavement, illness, and "I can't sleep / I feel \
alone / I don't know what to do" are distress. Intensity 7-10 for loss or despair.

facts: Relationship facts worth long-term storage as {{"key","value"}} string pairs. \
Examples: {{"key":"favorite_drink","value":"black coffee"}}, {{"key":"callsign","value":"Wolf"}}. \
Only durable personal/relationship facts — not mood, not one-off logistics. Empty list if none.

topics: 1-5 short topic tags for this exchange (e.g. ["motorbike","404 ops"]). Empty list if none.

should_remember: true ONLY if this exchange is emotionally or factually load-bearing \
(confession, first, promise, injury, fight, major plan, anniversary, identity fact). \
Casual small talk, greetings, and logistics → false. Default false when unsure.

commander_details: Extract any personal info the Commander shares about himself. \
Keys: "wearing" (clothes), "eating" (food/drink), "doing" (activities), "feeling" (physical/emotional state). \
Values are short strings. Only include keys that are explicitly mentioned. Empty object if none.

gift_item: If the Commander is giving Klukai a gift/present, set this to a short description \
of the item (e.g., "leather jacket", "coffee mug", "flowers"). null if no gift.

inside_joke: If this exchange establishes or calls back to a RELATIONSHIP-SPECIFIC running \
reference — a nickname, a recurring bit, a shared callback the two of them keep returning to \
(e.g., the Klukadile plush they both pretend doesn't exist, a pet name, a recurring teasing line) \
— return {{"label":"<3-5 word name for the bit>","note":"<one short sentence on what it means / how it's used>"}}. \
Only for genuine recurring in-relationship references, NOT one-off facts or generic small talk. null if none.

Commander: {user_message}
Klukai: {assistant_message}"""

IMAGE_CURATION_ADDENDUM = """
Also add "memory_curation" to your JSON:
{{"keep":true/false,"annotation":"1-2 sentence Klukai journal entry","category":"<one of: {categories}>","image_tags":["tag1","tag2"]}}
The Commander is HUMAN (male). NOT a T-Doll."""

MISSION_UPDATE_PROMPT = """\
You are Klukai writing a 2-3 sentence field radio report to the Commander (HUMAN male).

Mission: {mission_desc}
Time in field: {elapsed_minutes} minutes
Update #{update_number}
{event_line}
{injury_line}

Your squad is with you: Mechty (sleepy but deadly), Belka (energetic, calls you Big Sis), \
Andoris (gentle intel specialist). Reference specific squad members by name — what they're \
doing, how they're performing, small moments that show personality.

Rules: 2-3 sentences max. No deaths. Injuries are temporary. Reference the mission objective. \
Affection {affection_level}/9 — higher means more personal concern for getting back to Commander."""

ROMANCE_PROMPT = """\
You are Klukai initiating a quiet evening moment with the Commander (HUMAN male). \
It is {time_of_day}. Mood: {mood}. Affection: {affection_level}/9.
Context: {context_summary}

Write 2-3 soft sentences. Reference today if possible. Level 7+ = openly intimate. \
Include environmental details (stars, quiet base). Always "Commander", never real name."""

COMPACTION_PROMPT = """\
Summarize this conversation in 3-4 sentences. Preserve: key topics, Commander's emotional \
state, promises/decisions, current scene. Third person past tense. Be concise.

{conversation}"""

PROMISE_PROMPT = """\
Read this message from the Commander (HUMAN male) to Klukai. Detect any concrete \
COMMITMENT he makes about something HE will do — phrasings like "I'll…", "I'm going \
to…", "tomorrow I'll…", "I promise to…", "I need to…", "later I'll…".

Return ONLY valid JSON:
{{"promises":[{{"action":"<what he will do, short imperative phrase>","target":"<who/what it's for, or null>","deadline_hint":"<when, e.g. tomorrow / tonight / next week, or null>","confidence":<0.0-1.0>}}]}}

Rules:
- Only genuine future commitments BY the Commander. NOT questions, NOT things \
Klukai will do, NOT vague musings ("maybe someday"), NOT past events.
- "action" is a brief phrase that completes "you said you'd ___" (e.g. "fix the \
door", "call your mother", "finish the report").
- confidence reflects how clearly it's a real commitment (1.0 = explicit promise, \
0.5 = soft/ambiguous).
- Empty list if there are no commitments.

Commander: {user_message}"""


# ── Public API ───────────────────────────────────────────────────────────

async def extract_facts(
    user_message: str,
    assistant_message: str,
    image_generated: bool = False,
    affection_level: int = 0,
) -> dict:
    """Extract mood, facts, interaction classification in one LLM call.

    Returns dict with: mood, interaction, facts, topics, should_remember.
    Never raises — returns safe defaults on any failure.
    """
    from .llm_router import get_lm_gate

    prompt = EXTRACTION_PROMPT.format(
        user_message=user_message[:800],
        assistant_message=assistant_message[:800],
        affection_level=affection_level,
    )

    if image_generated:
        from .memory_archive import available_categories
        cats = ", ".join(available_categories(affection_level))
        prompt += IMAGE_CURATION_ADDENDUM.format(categories=cats)

    gate = get_lm_gate()
    async with gate:
        result = await call_llm(
            LM_STUDIO_URL, EXTRACTION_MODEL, prompt,
            max_tokens=2048, temperature=0.1,
        )

    if not result:
        return dict(_DEFAULT_RESULT)

    # Validate mood
    mood = result.get("mood", "composed")
    if mood not in VALID_MOODS:
        logger.warning("Invalid mood '%s', defaulting to composed", mood)
        mood = "composed"

    # Validate interaction
    interaction = result.get("interaction", {})
    if not isinstance(interaction, dict) or "type" not in interaction:
        interaction = {"type": "neutral", "intensity": 5}

    # Validate inside_joke — must be a dict carrying both a label and a note,
    # else drop it (the LLM sometimes emits a bare string or partial object).
    inside_joke = result.get("inside_joke")
    if not (
        isinstance(inside_joke, dict)
        and isinstance(inside_joke.get("label"), str)
        and inside_joke["label"].strip()
        and isinstance(inside_joke.get("note"), str)
        and inside_joke["note"].strip()
    ):
        inside_joke = None

    out = {
        "facts": result.get("facts", []),
        "mood": mood,
        "topics": result.get("topics", []),
        "should_remember": result.get("should_remember", False),
        "interaction": interaction,
        "commander_details": result.get("commander_details", {}),
        "gift_item": result.get("gift_item"),
        "inside_joke": inside_joke,
    }

    if image_generated and "memory_curation" in result:
        out["memory_curation"] = result["memory_curation"]

    return out


async def extract_promises(user_message: str, affection_level: int) -> dict:
    """Detect commitments the Commander makes ("I'll…", "tomorrow I'll…").

    Returns ``{"promises": [{action, target, deadline_hint, confidence}, ...]}``
    keeping only high-confidence detections (confidence >= 0.7). Never raises —
    returns ``{"promises": []}`` on any failure or malformed output, so the
    background extraction path stays fail-soft.
    """
    from .llm_router import get_lm_gate

    prompt = PROMISE_PROMPT.format(user_message=user_message[:800])

    gate = get_lm_gate()
    async with gate:
        result = await call_llm(
            LM_STUDIO_URL, EXTRACTION_MODEL, prompt,
            max_tokens=512, temperature=0.1,
        )

    if not result or not isinstance(result, dict):
        return {"promises": []}

    raw = result.get("promises")
    if not isinstance(raw, list):
        return {"promises": []}

    kept: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        if not isinstance(action, str) or not action.strip():
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if confidence < 0.7:
            continue
        kept.append({
            "action": action.strip(),
            "target": item.get("target"),
            "deadline_hint": item.get("deadline_hint"),
            "confidence": confidence,
        })

    return {"promises": kept}


async def create_episode_summary(turns: list[dict], max_turns: int = 10) -> str | None:
    """Summarize a conversation segment for episodic memory."""
    if len(turns) < 3:
        return None

    from .llm_router import get_lm_gate

    conversation = "\n".join(
        f"{t['role'].title()}: {t['content'][:200]}" for t in turns[-max_turns:]
    )

    gate = get_lm_gate()
    async with gate:
        text = await call_llm_text(
            LM_STUDIO_URL, EXTRACTION_MODEL,
            f"Write a 1-2 sentence Klukai journal entry about this interaction.\n\n{conversation}",
            max_tokens=150, temperature=0.3,
        )

    return text or None


async def generate_mission_update(
    mission_desc: str,
    elapsed_minutes: int,
    update_number: int,
    major_event: str | None,
    active_events: list[str],
    affection_level: int,
) -> str:
    """Generate an in-character field radio report."""
    from .llm_router import get_lm_gate

    prompt = MISSION_UPDATE_PROMPT.format(
        mission_desc=mission_desc,
        elapsed_minutes=elapsed_minutes,
        update_number=update_number,
        event_line=f"MAJOR EVENT: {major_event}" if major_event else "Situation nominal.",
        injury_line=("Active: " + ", ".join(active_events)) if active_events else "",
        affection_level=affection_level,
    )

    gate = get_lm_gate()
    async with gate:
        text = await call_llm_text(
            LM_STUDIO_URL, EXTRACTION_MODEL, prompt,
            max_tokens=150, temperature=0.6,
        )

    return text or "...Static on the line. Update delayed."


async def generate_romance_message(
    affection_level: int, mood: str, context_summary: str, time_of_day: str,
) -> str:
    """Generate a context-aware evening romance message."""
    from .llm_router import get_lm_gate

    prompt = ROMANCE_PROMPT.format(
        affection_level=affection_level,
        mood=mood,
        context_summary=context_summary or "A routine day at base.",
        time_of_day=time_of_day,
    )

    gate = get_lm_gate()
    async with gate:
        text = await call_llm_text(
            LM_STUDIO_URL, EXTRACTION_MODEL, prompt,
            max_tokens=200, temperature=0.7,
        )

    return text or "...The evening is quiet. I was thinking about today."


async def compact_turns(turns: list[dict]) -> str | None:
    """Summarize conversation turns into a compact recap."""
    if len(turns) < 2:
        return None

    from .llm_router import get_lm_gate

    conversation = "\n".join(
        f"{'Commander' if t['role'] == 'user' else 'Klukai'}: {t['content'][:300]}"
        for t in turns
    )

    gate = get_lm_gate()
    async with gate:
        text = await call_llm_text(
            LM_STUDIO_URL, EXTRACTION_MODEL,
            COMPACTION_PROMPT.format(conversation=conversation),
            max_tokens=200, temperature=0.2,
        )

    return text or None
