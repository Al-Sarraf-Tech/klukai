"""Legacy affection classification provider boundary, separate from scoring."""

from __future__ import annotations

import json
import logging
import httpx

from .dependencies import http_request
from .lm_gateway import LM_TTL_SECONDS, lm_studio_auth_headers, lm_timeout

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
    client: httpx.AsyncClient | None,
    user_message: str,
    *,
    url: str,
    model: str,
    prompt_template: str,
) -> tuple[str, int]:
    """Use LLM to classify the Commander's message type and intensity.

    On the shared background route (app/background_llm.py): Haiku when
    enabled, else or on failure the local request while Dominus is up, else
    ("neutral", 5). Never raises.
    """
    from . import background_llm

    prompt = prompt_template.format(
        user_message=user_message[:500],
    )

    async def local() -> object:
        from .llm_router import get_lm_gate

        gate = get_lm_gate()
        async with gate:  # Waits for main chat to finish streaming
            if client is None:
                raise RuntimeError("Affection client has not been initialized")
            r = await http_request(
                "lm_studio",
                client,
                "post",
                f"{url}/v1/chat/completions",
                headers=lm_studio_auth_headers(),
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 100,
                    "temperature": 0.1,
                    "stream": False,
                    "ttl": LM_TTL_SECONDS,
                },
                timeout=lm_timeout(12.0),
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"].strip()

        # Handle markdown code blocks
        if content.startswith("```"):
            content = content.split("\n", 1)[1]
            content = content.rsplit("```", 1)[0]

        return json.loads(content)

    result = await background_llm.json_object(
        "affection_classification",
        prompt,
        max_tokens=100,
        local=local,
        local_model=model,
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
