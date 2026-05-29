"""Reflection-on-return helper — extracted from app/chat_handlers.py.

S+ Phase 2 §6.1 file-size hygiene."""

from __future__ import annotations

import logging

from .context import (
    affection,
    router,
    ws,
)

logger = logging.getLogger(__name__)

REFLECTION_MIN_HOURS_AWAY = 8
REFLECTION_MAX_HOURS_AWAY = 72

async def _maybe_oath_on_connect(user_id: str) -> None:
    """On connect, if the Commander is already at the level-9 "Oath Fulfilled"
    tier and the one-time oath has never fired (e.g. they reached lv9 before the
    capstone feature existed), deliver it now. Self-guarded — fires once ever.
    """
    try:
        if (await affection.get_state(user_id)).level < 9:
            return
        from .background import maybe_deliver_oath
        await maybe_deliver_oath(user_id)
    except Exception as e:
        logger.debug("Oath-on-connect skipped: %s", e)


async def _maybe_reflect_on_return(user_id: str) -> None:
    """Greet the returning user with a reference to the last topic if away >8h.

    Pulled from companion_messages. Silent fail on any error — this is a
    best-effort nicety.
    """
    try:
        from datetime import datetime, timezone
        from .db import get_pool

        pool = get_pool()
        last_at = None
        recent_excerpts: list[tuple[str, str]] = []
        prior_mood: str | None = None
        async with pool.connection() as conn:
            # Last message time
            row = await (await conn.execute(
                "SELECT MAX(created_at) FROM companion_messages WHERE user_id = %s",
                (user_id,),
            )).fetchone()
            if not row or not row[0]:
                return  # Brand new user — let them lead
            last_at = row[0]

            # Last 6 exchanges for context
            rows = await (await conn.execute(
                "SELECT role, content FROM companion_messages "
                "WHERE user_id = %s ORDER BY created_at DESC LIMIT 6",
                (user_id,),
            )).fetchall()
            recent_excerpts = [(r[0], r[1][:250]) for r in reversed(rows)]

            # Prior mood — carry her last emotional register into the new
            # session instead of resetting to neutral.
            try:
                mrow = await (await conn.execute(
                    "SELECT mood FROM companion_persistent_state WHERE user_id = %s",
                    (user_id,),
                )).fetchone()
                if mrow and mrow[0]:
                    prior_mood = mrow[0]
            except Exception as me:
                logger.debug("Prior-mood lookup skipped: %s", me)

        # Compute hours away
        now = datetime.now(timezone.utc)
        if last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)
        hours_away = (now - last_at).total_seconds() / 3600

        # Classify the greeting kind (dream / reflection / silent)
        from .character_behaviors import (
            classify_return_greeting,
            compose_return_emotion,
        )
        local_hour = datetime.now().hour
        kind = classify_return_greeting(
            hours_away=hours_away,
            local_hour=local_hour,
            min_hours=REFLECTION_MIN_HOURS_AWAY,
            max_hours=REFLECTION_MAX_HOURS_AWAY,
        )
        if kind == "silent":
            return
        if not recent_excerpts:
            return

        # Small delay so the UI settles before the greeting arrives
        import asyncio
        await asyncio.sleep(3)

        excerpt_text = "\n".join(f"{role}: {content}" for role, content in recent_excerpts)
        from .personality import load_personality, build_character_preamble
        p = load_personality()
        aff_state = await affection.get_state(user_id)
        system_prompt = build_character_preamble(p, aff_state.level)

        # Emotional continuity: how the gap *felt* to her, scaled by closeness
        # and gap length, with her prior mood carried over so the new session
        # resumes her last register instead of resetting to neutral.
        emotion_beat = compose_return_emotion(
            hours_away=hours_away,
            affection_level=aff_state.level,
            prior_mood=prior_mood,
        )
        if kind == "dream":
            user_prompt = (
                f"Commander just reconnected in the morning after being away "
                f"about {int(hours_away)} hours. Open by telling them — warmly, "
                "briefly (2-3 sentences) — about a dream you had that was loosely "
                "inspired by your last conversation. First-person. Don't describe "
                "the dream as fiction — you lived it. No bullet points.\n\n"
                f"How the time apart felt to you: {emotion_beat}\n\n"
                "Last conversation excerpt:\n" + excerpt_text
            )
        else:
            user_prompt = (
                f"Commander just reconnected after being away for about {int(hours_away)} hours. "
                "Greet them in 2-3 sentences. Pick the conversational thread back up: "
                "reference the specific topic from the last exchanges below — what he was in "
                "the middle of telling you, or a feeling you'd been left with — and invite him "
                "to continue it. Do not use bullet points. Stay in first-person.\n\n"
                f"How the time apart felt to you: {emotion_beat}\n\n"
                "Last exchanges (resume from here):\n" + excerpt_text
            )

        import os
        from .models import LLMConfig
        config = LLMConfig(
            provider="lmstudio",
            model="cognitivecomputations_dolphin-mistral-24b-venice-edition",
            base_url=os.environ.get("LM_STUDIO_URL", "http://192.168.50.2:1234"),
            temperature=0.9,
            max_tokens=180,
        )
        resp = await router.complete_local(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            config=config,
        )
        greeting = (
            resp.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        if not greeting or len(greeting) < 10:
            return

        # If this was a dream, persist it as a memory archive entry
        if kind == "dream":
            try:
                from . import dreams
                mood_now = prior_mood or getattr(aff_state, "mood", "tender")
                await dreams.save_dream(
                    dream_text=greeting,
                    user_id=user_id,
                    affection_level=aff_state.level,
                    mood=mood_now,
                )
            except Exception as e:
                logger.warning("Dream save skipped: %s", e)

        # Deliver via WS if still connected
        if ws.is_connected(user_id):
            await ws.send_proactive(user_id, greeting)
            logger.info(
                "Reflection-on-return sent to %s (away %dh kind=%s): %s",
                user_id, int(hours_away), kind, greeting[:60]
            )
    except Exception as e:
        logger.warning("Reflection-on-return failed: %s", e)
