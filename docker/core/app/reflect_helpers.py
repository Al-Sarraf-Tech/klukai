"""Reflection-on-return helper — extracted from app/chat_handlers.py.

S+ Phase 2 §6.1 file-size hygiene."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .context import (
    affection,
    router,
    ws,
)
from .proactive.state import now_local

if TYPE_CHECKING:
    from .affection import AffectionState

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
        last_at, recent_excerpts, prior_mood = await _load_return_context(user_id)
        if last_at is None:
            return  # Brand new user — let them lead

        # Compute hours away
        hours_away = _hours_since(last_at)

        # Classify the greeting kind (dream / reflection / silent)
        kind = _classify_return(hours_away)
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

        user_prompt = _compose_return_prompt(
            kind, hours_away, aff_state, prior_mood, excerpt_text,
        )
        greeting = await _generate_return_greeting(system_prompt, user_prompt)
        if not greeting or len(greeting) < 10:
            return

        # If this was a dream, persist it as a memory archive entry
        if kind == "dream":
            await _save_dream_greeting(greeting, user_id, aff_state, prior_mood)

        # Deliver via WS if still connected
        if ws.is_connected(user_id):
            await _deliver_return_greeting(
                user_id, greeting, hours_away, kind, aff_state, prior_mood,
            )
    except Exception as e:
        logger.warning("Reflection-on-return failed: %s", e)


async def _load_return_context(
    user_id: str,
) -> tuple[Any, list[tuple[str, str]], str | None]:
    """Read the Commander's last-message time, recent exchanges and prior mood.

    Returns ``(last_at, recent_excerpts, prior_mood)``; ``last_at`` is ``None``
    for a brand-new user with no history, in which case nothing else is read.
    """
    from .db import get_pool

    pool = get_pool()
    recent_excerpts: list[tuple[str, str]] = []
    prior_mood: str | None = None
    async with pool.connection() as conn:
        # Last message time. Exclude her own proactive check-ins: they are
        # persisted to history too, and counting them would measure the gap
        # since *she* last spoke rather than since the Commander did —
        # shrinking hours_away and suppressing the return greeting entirely.
        row = await (await conn.execute(
            "SELECT MAX(created_at) FROM companion_messages "
            "WHERE user_id = %s AND COALESCE(model, '') <> 'proactive'",
            (user_id,),
        )).fetchone()
        if not row or not row[0]:
            return None, recent_excerpts, prior_mood  # Brand new user — let them lead
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
    return last_at, recent_excerpts, prior_mood


def _hours_since(last_at: Any) -> float:
    """Hours between ``last_at`` and now; a naive timestamp is taken as UTC."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)
    return (now - last_at).total_seconds() / 3600


def _classify_return(hours_away: float) -> str:
    """Classify the greeting kind (dream / reflection / silent) for this gap."""
    from .character_behaviors import classify_return_greeting

    # The Commander's wall-clock hour (America/Chicago) — the server runs UTC.
    local_hour = now_local().hour
    return classify_return_greeting(
        hours_away=hours_away,
        local_hour=local_hour,
        min_hours=REFLECTION_MIN_HOURS_AWAY,
        max_hours=REFLECTION_MAX_HOURS_AWAY,
    )


def _compose_return_prompt(
    kind: str,
    hours_away: float,
    aff_state: AffectionState,
    prior_mood: str | None,
    excerpt_text: str,
) -> str:
    """Build the user prompt for the return greeting of the given ``kind``."""
    from .character_behaviors import compose_return_emotion

    # Emotional continuity: how the gap *felt* to her, scaled by closeness
    # and gap length, with her prior mood carried over so the new session
    # resumes her last register instead of resetting to neutral.
    emotion_beat = compose_return_emotion(
        hours_away=hours_away,
        affection_level=aff_state.level,
        prior_mood=prior_mood,
    )
    if kind == "dream":
        return (
            f"Commander just reconnected in the morning after being away "
            f"about {int(hours_away)} hours. Open by telling them — warmly, "
            "briefly (2-3 sentences) — about a dream you had that was loosely "
            "inspired by your last conversation. First-person. Don't describe "
            "the dream as fiction — you lived it. No bullet points.\n\n"
            f"How the time apart felt to you: {emotion_beat}\n\n"
            "Last exchanges (resume from here):\n" + excerpt_text
        )
    return (
        f"Commander just reconnected after being away for about {int(hours_away)} hours. "
        "Greet them in 2-3 sentences. Pick the conversational thread back up: "
        "reference the specific topic from the last exchanges below — what he was in "
        "the middle of telling you, or a feeling you'd been left with — and invite him "
        "to continue it. Do not use bullet points. Stay in first-person.\n\n"
        f"How the time apart felt to you: {emotion_beat}\n\n"
        "Last exchanges (resume from here):\n" + excerpt_text
    )


async def _generate_return_greeting(system_prompt: str, user_prompt: str) -> str:
    """Ask the local model for the greeting; returns the stripped text."""
    import os
    from .models import LLMConfig
    config = LLMConfig(
        provider="lmstudio",
        model="cognitivecomputations_dolphin-mistral-24b-venice-edition",
        base_url=os.environ.get("LM_STUDIO_URL", "http://100.107.121.5:1234"),
        temperature=0.9,
        max_tokens=180,
    )
    resp = await router.complete_local(
        system_prompt=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        config=config,
    )
    return (
        resp.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )


async def _save_dream_greeting(
    greeting: str,
    user_id: str,
    aff_state: AffectionState,
    prior_mood: str | None,
) -> None:
    """Persist a dream greeting as a memory archive entry. Fail-soft."""
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


async def _deliver_return_greeting(
    user_id: str,
    greeting: str,
    hours_away: float,
    kind: str,
    aff_state: AffectionState,
    prior_mood: str | None,
) -> None:
    """Send the greeting over WS as a voice letter, or as plain text on failure."""
    # Voice letter: try to synthesize the greeting as a JP voice note she
    # "left" while he was away. On success, send a voice_letter payload so
    # the client can play the audio; on failure (voice service down, disk,
    # DB) save_voice_note returns None and we fall back to the unchanged
    # plain-text proactive path below. Wrapped so a voice-path error can
    # never suppress the text greeting.
    voice_id = None
    try:
        from .voice_archive import save_voice_note
        mood_now = prior_mood or getattr(aff_state, "mood", None)
        voice_id = await save_voice_note(
            greeting,
            user_id=user_id,
            kind="reflection",
            mood=mood_now,
            affection_level=aff_state.level,
        )
    except Exception as ve:
        logger.warning("Voice letter attempt failed for %s: %s", user_id, ve)
        voice_id = None

    if voice_id:
        await ws.send(user_id, {
            "type": "voice_letter",
            "voice_id": voice_id,
            "message": greeting,
        })
    else:
        # Fallback — existing text path, unchanged.
        await ws.send_proactive(user_id, greeting)
    logger.info(
        "Reflection-on-return sent to %s (away %dh kind=%s voice=%s): %s",
        user_id, int(hours_away), kind, bool(voice_id), greeting[:60]
    )
