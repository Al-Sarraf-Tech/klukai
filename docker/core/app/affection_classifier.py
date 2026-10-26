"""Legacy affection classification provider boundary, separate from scoring."""

from __future__ import annotations

import logging

CLASSIFICATION_PROMPT = """\
Classify ONLY the Commander's message below. Ignore Klukai's response tone entirely.
The Commander is talking to a cold military character — direct or casual language is NORMAL.

Choose ONE type and rate intensity (1-10):
- "greeting": Hello, goodbye, check-in, how are you (intensity 1-3)
- "genuine_interest": Asking about Klukai, her missions, squad, wellbeing, history (intensity 1-5)
- "personal_sharing": Commander sharing personal details or feelings (intensity 1-5)
- "compliment": Praise, admiration, appreciation of Klukai (intensity 3-8)
- "mission_discussion": Tactical, operational, strategic, or factual conversation (intensity 1-5)
- "remembering": Recalling something from a previous conversation (intensity 3-5)
- "rude": ONLY explicit insults, slurs, "shut up", "you're useless", "I don't need you" (intensity 5-10)
- "inappropriate": ONLY sexually explicit, objectifying, or degrading content (intensity 5-10)
- "ignoring_advice": ONLY explicitly saying "no I won't do that" to Klukai's direct counsel (intensity 3-5)
- "neutral": Normal conversation, questions, small talk (intensity 1-3)

IMPORTANT: When in doubt, classify as "neutral" or "genuine_interest". Short messages, questions,
and casual conversation are NEVER "rude". Only classify as "rude" if the Commander is clearly hostile.

Return ONLY valid JSON: {{"type": "...", "intensity": N}}

Commander's message: {user_message}
"""

logger = logging.getLogger(__name__)


async def classify_interaction(
    user_message: str,
    *,
    prompt_template: str = CLASSIFICATION_PROMPT,
) -> tuple[str, int]:
    """Use the background model to classify the Commander's message.

    On the shared background route (app/background_llm.py): Claude Haiku,
    else ("neutral", 5). Never raises.
    """
    from . import background_llm

    prompt = prompt_template.format(
        user_message=user_message[:500],
    )
    result = await background_llm.json_object(
        "affection_classification", prompt, max_tokens=100
    )
    try:
        if result is None:
            raise ValueError("no classification")
        itype = result.get("type", "neutral")
        intensity = max(1, min(10, int(result.get("intensity", 5))))
        return itype, intensity
    except Exception as e:
        logger.warning("Interaction classification failed (%s)", type(e).__name__)
        return "neutral", 5
