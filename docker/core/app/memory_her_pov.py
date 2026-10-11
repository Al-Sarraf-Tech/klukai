"""Her POV memory portraits — she picks a moment, journals it, draws it.

User-initiated feature: Commander opens the Her POV menu and asks her to
find *any* real exchange from their history, write a short journal line in
character, and render a picture from her side of the moment. The result is
persisted as a kept Precious Memory tagged ``her_pov``.

The job rail (in-process board, Postgres write-through, queue hand-off and
worker claim) lives in :mod:`.memory_her_pov_jobs`. Its names are re-exported
below because routes, the deferred runner and the tests reach them through
this module.
"""

from __future__ import annotations

import asyncio
import os
import base64
import logging
import random
import re
import uuid
from datetime import datetime  # noqa: F401  (tests patch memory_her_pov.datetime)
from typing import Any

from .memory_her_pov_jobs import (  # noqa: F401  (re-exports — see module docstring)
    _ACTIVE_JOB_BY_USER,
    _JOB_MAX,
    _JOB_TTL_SECONDS,
    _JOBS,
    _JOBS_LOCK,
    _STALE_CLAIM_MINUTES,
    _STALE_JOB_MINUTES,
    _claim_active_from_db,
    _enqueue_job,
    _load_job_from_db,
    _now_iso,
    _persist_job,
    _set_job,
    _start_inline,
    get_job,
    run_job_from_queue,
)

logger = logging.getLogger(__name__)

_TRIVIAL = {
    "ok", "okay", "yes", "no", "yeah", "yep", "nope", "sure", "thanks",
    "thank you", "hi", "hey", "hello", "good", "nice", "cool", "hm", "hmm",
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
        rows = await (await conn.execute(
            "SELECT id, role, content, mood, model, created_at "
            "FROM companion_messages "
            "WHERE user_id = %s AND role IN ('user', 'assistant') "
            "AND COALESCE(model, '') <> 'proactive' "
            "ORDER BY created_at DESC LIMIT 120",
            (user_id,),
        )).fetchall()

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
                "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created),
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
            candidates.append({
                **pending_user,
                "assistant_content": content[:1200],
                "user_content": pending_user["user_content"][:800],
                "mood": mood or "composed",
                "score": score,
            })
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


async def compose_pov(
    exchange: dict[str, Any],
    affection_level: int,
) -> dict[str, Any]:
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
    raw: dict = {}
    try:
        async with get_lm_gate():
            raw = await call_llm(
                LM_STUDIO_URL, model, prompt,
                max_tokens=512, temperature=0.4,
            )
    except Exception as e:
        logger.warning("compose_pov LLM failed: %s", e)
        raw = {}

    if not isinstance(raw, dict):
        raw = {}

    annotation = raw.get("annotation")
    if not isinstance(annotation, str) or len(annotation.strip()) < 8:
        annotation = (
            "...I pulled this from our records. Don't read into why I kept it."
        )
    scene_tags = raw.get("scene_tags")
    if not isinstance(scene_tags, str) or len(scene_tags.strip()) < 8:
        scene_tags = (
            "indoors, soft lighting, silver hair, high ponytail, "
            "tactical outfit, looking at viewer, serious expression"
        )
    couple = bool(raw.get("couple")) if isinstance(raw.get("couple"), bool) else False
    mood = raw.get("mood") if isinstance(raw.get("mood"), str) else exchange.get("mood", "composed")
    title = raw.get("title") if isinstance(raw.get("title"), str) else "From my side"
    title = re.sub(r"\s+", " ", title).strip()[:48] or "From my side"

    return {
        "annotation": annotation.strip()[:500],
        "scene_tags": scene_tags.strip()[:600],
        "couple": couple,
        "mood": mood.strip()[:40] or "composed",
        "title": title,
    }


async def run_her_pov(user_id: str, job_id: str) -> None:
    """Full pipeline with WS status updates. Never raises out of task."""
    from .context import affection
    from . import memory_archive
    from .context import ws
    from .image_gen import INTIMATE_MIN_LEVEL, build_prompt, generate_image
    from .her_day import render_costume

    try:
        await _set_job(job_id, status="searching", phase="searching",
                       message="Searching our records…")
        try:
            await ws.send_thinking(user_id, "Searching our records…")
            await ws.send(user_id, {
                "type": "her_pov",
                "job_id": job_id,
                "status": "searching",
                "phase": "searching",
                "message": "Searching our records…",
            })
        except Exception:
            pass

        exchange = await pick_exchange(user_id)
        if not exchange:
            await _set_job(
                job_id, status="failed", phase="failed",
                error="no_history",
                message="…Our records are still thin. Talk to me more first.",
            )
            try:
                await ws.send_proactive(
                    user_id,
                    "…Our records are still thin. Talk to me more first, Commander.",
                    persist=False,
                )
                await ws.send(user_id, {
                    "type": "her_pov", "job_id": job_id, "status": "failed",
                    "phase": "failed", "error": "no_history",
                })
            except Exception:
                pass
            return

        aff = await affection.get_state(user_id)
        level = aff.level

        await _set_job(job_id, status="thinking", phase="thinking",
                       message="Replaying the moment…",
                       exchange_preview={
                           "user": exchange["user_content"][:160],
                           "assistant": exchange["assistant_content"][:160],
                       })
        try:
            await ws.send_thinking(user_id, "Replaying the moment from my side…")
            await ws.send(user_id, {
                "type": "her_pov", "job_id": job_id, "status": "thinking",
                "phase": "thinking",
            })
        except Exception:
            pass

        pov = await compose_pov(exchange, level)

        await _set_job(job_id, status="drawing", phase="drawing",
                       message="Sketching from my side…",
                       title=pov["title"], annotation=pov["annotation"])
        try:
            await ws.send_thinking(user_id, "Sketching from my side…")
            await ws.send(user_id, {
                "type": "her_pov", "job_id": job_id, "status": "drawing",
                "phase": "drawing", "title": pov["title"],
            })
        except Exception:
            pass

        # She draws herself in what she has on today.
        costume = await render_costume(user_id, level)

        # The Commander's wall clock, not the container's UTC — otherwise an
        # evening portrait gets rendered with 1am lighting.
        from .proactive.state import now_local
        hour = now_local().hour
        if 5 <= hour < 12:
            tod = "morning"
        elif 12 <= hour < 17:
            tod = "afternoon"
        elif 17 <= hour < 21:
            tod = "evening"
        else:
            tod = "night"

        prompt = build_prompt(
            pov["scene_tags"],
            couple=pov["couple"],
            affection_level=level,
            context=f"{exchange['user_content']} {exchange['assistant_content']}"[:400],
            mood=pov["mood"],
            time_of_day=tod,
            costume=costume,
        )

        img = await generate_image(prompt, sfw=level < INTIMATE_MIN_LEVEL)
        if not img:
            await _set_job(
                job_id, status="failed", phase="failed",
                error="image_failed",
                message="…Visualization failed. Interference in the rendering pipeline.",
                annotation=pov["annotation"], title=pov["title"],
            )
            try:
                await ws.send_proactive(
                    user_id,
                    "…Visualization failed. Interference in the rendering pipeline. "
                    "I'll try again later.",
                    persist=False,
                )
                await ws.send(user_id, {
                    "type": "her_pov", "job_id": job_id, "status": "failed",
                    "phase": "failed", "error": "image_failed",
                })
            except Exception:
                pass
            return

        memory_id = await memory_archive.save_image(
            image_bytes=img,
            prompt=prompt,
            conversation_id=f"her_pov:{job_id}",
            mood=pov["mood"],
            affection_level=level,
            curation={
                "keep": True,
                "annotation": pov["annotation"],
                "category": "Precious Memories" if level >= 6 else "The Commander" if level >= 3 else "Quiet Hours",
                "image_tags": ["her_pov", "from_her_side", "commander_request"],
            },
            user_id=user_id,
        )
        if not memory_id:
            # save_image returns None when the archive rejects the row (e.g. the
            # annotation deduped against an earlier one) and deletes the files.
            # Reporting "done" here left the client showing "Kept." over an
            # empty stage with nothing in the archive.
            await _set_job(
                job_id, status="failed", phase="failed",
                error="not_saved",
                message="…I drew it, but it wouldn't keep. Ask me again.",
                annotation=pov["annotation"], title=pov["title"],
            )
            try:
                await ws.send(user_id, {
                    "type": "her_pov", "job_id": job_id, "status": "failed",
                    "phase": "failed", "error": "not_saved",
                })
            except Exception:
                pass
            return

        img_b64 = base64.b64encode(img).decode()
        await _set_job(
            job_id,
            status="done",
            phase="done",
            message="Kept.",
            memory_id=memory_id,
            title=pov["title"],
            annotation=pov["annotation"],
            mood=pov["mood"],
            # do not store full image b64 in job forever — client gets WS once
            has_image=True,
        )

        try:
            line = pov["annotation"]
            await ws.send_proactive(user_id, line)
            await asyncio.sleep(0.8)
            await ws.send(user_id, {
                "type": "image",
                "data": img_b64,
                "memory_id": memory_id,
            })
            await ws.send(user_id, {
                "type": "her_pov",
                "job_id": job_id,
                "status": "done",
                "phase": "done",
                "memory_id": memory_id,
                "title": pov["title"],
                "annotation": pov["annotation"],
                "mood": pov["mood"],
            })
        except Exception as e:
            logger.warning("her_pov WS deliver failed: %s", e)

        logger.info("her_pov done user=%s job=%s mem=%s", user_id, job_id, memory_id)

    except asyncio.CancelledError:
        # WSManager.disconnect cancels tracked tasks when the last device drops,
        # and this pipeline runs for minutes. CancelledError is not an Exception
        # subclass, so without this the job would sit at "drawing" forever and
        # the client would poll it for the life of the process.
        logger.info("her_pov cancelled user=%s job=%s", user_id, job_id)
        await _set_job(
            job_id, status="failed", phase="failed",
            error="cancelled", message="…Interrupted. Ask me again when you're back.",
        )
        raise
    except Exception as e:
        logger.exception("her_pov failed: %s", e)
        await _set_job(
            job_id, status="failed", phase="failed",
            error="internal", message=str(e)[:200],
        )
        try:
            await ws.send(user_id, {
                "type": "her_pov", "job_id": job_id, "status": "failed",
                "phase": "failed", "error": "internal",
            })
        except Exception:
            pass
    finally:
        async with _JOBS_LOCK:
            if _ACTIVE_JOB_BY_USER.get(user_id) == job_id:
                _ACTIVE_JOB_BY_USER.pop(user_id, None)


def _execution_mode() -> str:
    """inline (default, unit tests) or queue (production durable rail)."""
    return (os.environ.get("HER_POV_EXECUTION") or "inline").strip().lower()


async def start_her_pov(user_id: str) -> dict[str, Any]:
    """Start a her-POV job if the user isn't already running one.

    One in-flight job per user. The claim is taken under the lock (and under a
    partial unique index in Postgres) before any work is scheduled, so a
    double-tapped CTA can never buy a second LLM call and a second GPU render.

    Production (`HER_POV_EXECUTION=queue`) only enqueues the job id onto the
    durable `klukai.jobs.her_pov` rail — the events-bridge consumer (prefetch=1)
    is the GPU lease. Unit tests and local dev keep the inline asyncio path.
    """
    existing = await _claim_active_from_db(user_id)
    if existing:
        return existing

    job_id = str(uuid.uuid4())
    async with _JOBS_LOCK:
        running = _ACTIVE_JOB_BY_USER.get(user_id)
        if running:
            job = _JOBS.get(running) or {}
            return {
                "job_id": running,
                "status": job.get("status", "queued"),
                "reused": True,
            }
        _ACTIVE_JOB_BY_USER[user_id] = job_id

    try:
        await _set_job(
            job_id,
            user_id=user_id,
            status="queued",
            phase="queued",
            message="Standing by…",
            created_at=_now_iso(),
        )
    except Exception:
        async with _JOBS_LOCK:
            if _ACTIVE_JOB_BY_USER.get(user_id) == job_id:
                _ACTIVE_JOB_BY_USER.pop(user_id, None)
        raise

    mode = _execution_mode()
    if mode == "queue":
        armed = await _enqueue_job(job_id)
        if not armed:
            # Fail-soft: run inline so a bridge outage never strands the CTA.
            logger.warning(
                "her_pov queue arm failed; falling back to inline for %s", job_id
            )
            return await _start_inline(user_id, job_id)
        return {"job_id": job_id, "status": "queued", "reused": False}

    return await _start_inline(user_id, job_id)
