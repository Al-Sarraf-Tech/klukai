"""Her POV memory portraits — she picks a moment, journals it, draws it.

User-initiated feature: Commander opens the Her POV menu and asks her to
find *any* real exchange from their history, write a short journal line in
character, and render a picture from her side of the moment. The result is
persisted as a kept Precious Memory tagged ``her_pov``.
"""

from __future__ import annotations

import asyncio
import os
import base64
import logging
import random
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# In-process job board (single-process uvicorn). Fail-soft if worker restarts.
_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = asyncio.Lock()
# user_id -> job_id of the run currently in flight. Authoritative for dedupe:
# scanning _JOBS for a non-terminal job raced with the WS delivery tail and let
# a double-tap start a second LLM call + GPU render.
_ACTIVE_JOB_BY_USER: dict[str, str] = {}

# The board is in-process and never persisted, so it must not grow forever.
_JOB_TTL_SECONDS = 3600.0
_JOB_MAX = 200

_TRIVIAL = {
    "ok", "okay", "yes", "no", "yeah", "yep", "nope", "sure", "thanks",
    "thank you", "hi", "hey", "hello", "good", "nice", "cool", "hm", "hmm",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_job(job_id: str) -> dict[str, Any] | None:
    """Return a public job view. Memory first, then Postgres.

    Postgres is the durable source of truth after a worker restart; the
    in-process board is a hot cache for the running pipeline and unit tests.
    """
    async with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job:
            return {k: v for k, v in job.items() if not k.startswith("_")}
    return await _load_job_from_db(job_id)


async def _load_job_from_db(job_id: str) -> dict[str, Any] | None:
    try:
        uuid.UUID(str(job_id))
    except (ValueError, AttributeError, TypeError):
        return None
    try:
        from .db import get_pool
        get_pool()  # raises if not initialized
    except Exception:
        return None
    try:
        from .db import get_conn
        async with get_conn() as conn:
            row = await (await conn.execute(
                "SELECT id, user_id, status, phase, message, error, title, "
                "annotation, mood, memory_id, has_image, exchange_preview, "
                "created_at, updated_at "
                "FROM companion_her_pov_jobs WHERE id = %s",
                (job_id,),
            )).fetchone()
        if not row:
            return None
        preview = row[11]
        if isinstance(preview, str):
            import json as _json
            try:
                preview = _json.loads(preview)
            except Exception:
                pass
        def _iso(v):
            return v.isoformat() if v is not None and hasattr(v, "isoformat") else v
        return {
            "id": str(row[0]),
            "user_id": row[1],
            "status": row[2],
            "phase": row[3],
            "message": row[4],
            "error": row[5],
            "title": row[6],
            "annotation": row[7],
            "mood": row[8],
            "memory_id": row[9],
            "has_image": bool(row[10]),
            "exchange_preview": preview,
            "created_at": _iso(row[12]),
            "updated_at": _iso(row[13]),
        }
    except Exception as e:
        logger.debug("her_pov load from db failed: %s", e)
        return None


def _prune_jobs_locked(now: float | None = None) -> None:
    """Drop stale jobs. Caller must hold _JOBS_LOCK.

    Entries carry titles, annotations and exchange previews, so an unbounded
    board is a slow leak for the lifetime of the worker.
    """
    now = time.monotonic() if now is None else now
    stale = [
        jid for jid, job in _JOBS.items()
        if now - float(job.get("_touched", now)) > _JOB_TTL_SECONDS
    ]
    for jid in stale:
        _JOBS.pop(jid, None)
    # Hard ceiling as a backstop for a burst inside one TTL window.
    if len(_JOBS) > _JOB_MAX:
        oldest = sorted(_JOBS.items(), key=lambda kv: kv[1].get("_touched", 0.0))
        for jid, _ in oldest[: len(_JOBS) - _JOB_MAX]:
            _JOBS.pop(jid, None)


async def _set_job(job_id: str, **fields: Any) -> None:
    async with _JOBS_LOCK:
        job = _JOBS.setdefault(job_id, {"id": job_id})
        job.update(fields)
        job["updated_at"] = _now_iso()
        job["_touched"] = time.monotonic()
        _prune_jobs_locked()
        snapshot = {k: v for k, v in job.items() if not k.startswith("_")}
    await _persist_job(job_id, snapshot)


async def _persist_job(job_id: str, snapshot: dict[str, Any]) -> None:
    """Best-effort write-through to Postgres. Never fails the pipeline."""
    try:
        from .db import get_pool
        get_pool()
    except Exception:
        return
    try:
        import json as _json
        from .db import get_conn_autocommit

        status = snapshot.get("status") or "queued"
        phase = snapshot.get("phase") or status
        terminal = status in ("done", "failed")
        preview = snapshot.get("exchange_preview")
        preview_json = _json.dumps(preview) if preview is not None else None
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "INSERT INTO companion_her_pov_jobs "
                "(id, user_id, status, phase, message, error, title, annotation, "
                " mood, memory_id, has_image, exchange_preview, created_at, updated_at, "
                " finished_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, "
                "        COALESCE(%s::timestamptz, NOW()), NOW(), "
                "        CASE WHEN %s THEN NOW() ELSE NULL END) "
                "ON CONFLICT (id) DO UPDATE SET "
                " status = EXCLUDED.status, "
                " phase = EXCLUDED.phase, "
                " message = EXCLUDED.message, "
                " error = EXCLUDED.error, "
                " title = COALESCE(EXCLUDED.title, companion_her_pov_jobs.title), "
                " annotation = COALESCE(EXCLUDED.annotation, companion_her_pov_jobs.annotation), "
                " mood = COALESCE(EXCLUDED.mood, companion_her_pov_jobs.mood), "
                " memory_id = COALESCE(EXCLUDED.memory_id, companion_her_pov_jobs.memory_id), "
                " has_image = EXCLUDED.has_image OR companion_her_pov_jobs.has_image, "
                " exchange_preview = COALESCE(EXCLUDED.exchange_preview, "
                "                             companion_her_pov_jobs.exchange_preview), "
                " updated_at = NOW(), "
                " finished_at = CASE WHEN %s THEN COALESCE(companion_her_pov_jobs.finished_at, NOW()) "
                "                    ELSE companion_her_pov_jobs.finished_at END",
                (
                    job_id,
                    snapshot.get("user_id") or "",
                    status,
                    phase,
                    snapshot.get("message"),
                    snapshot.get("error"),
                    snapshot.get("title"),
                    snapshot.get("annotation"),
                    snapshot.get("mood"),
                    snapshot.get("memory_id"),
                    bool(snapshot.get("has_image")),
                    preview_json,
                    snapshot.get("created_at"),
                    terminal,
                    terminal,
                ),
            )
    except Exception as e:
        logger.debug("her_pov persist failed: %s", e)


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
    from .context import memory, ws
    from .image_gen import build_prompt, generate_image, is_outfit_unlocked

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

        costume = await memory.recall_fact("costume", user_id)
        if not (costume and is_outfit_unlocked(costume, level)):
            costume = None

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

        img = await generate_image(prompt)
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


async def _enqueue_job(job_id: str) -> bool:
    """Ask the events-bridge to park this job on the durable work queue."""
    try:
        from . import events
        await events.publish(
            "job.enqueue",
            data=job_id,
            domain="job",
            kind="her_pov",
            job_id=job_id,
        )
        return True
    except Exception as e:
        logger.warning("her_pov enqueue failed for %s: %s", job_id, e)
        return False


async def _claim_active_from_db(user_id: str) -> dict[str, Any] | None:
    """Return an existing non-terminal DB job for this user, if any."""
    try:
        from .db import get_pool
        get_pool()
    except Exception:
        return None
    try:
        from .db import get_conn
        async with get_conn() as conn:
            row = await (await conn.execute(
                "SELECT id, status FROM companion_her_pov_jobs "
                "WHERE user_id = %s AND status NOT IN ('done', 'failed') "
                "ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            )).fetchone()
        if not row:
            return None
        return {"job_id": str(row[0]), "status": row[1], "reused": True}
    except Exception as e:
        logger.debug("her_pov db claim lookup failed: %s", e)
        return None


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


async def _start_inline(user_id: str, job_id: str) -> dict[str, Any]:
    """Legacy path: spawn the pipeline as an in-process asyncio task."""
    from .context import ws as _ws
    try:
        task = asyncio.create_task(run_her_pov(user_id, job_id))
    except Exception:
        async with _JOBS_LOCK:
            if _ACTIVE_JOB_BY_USER.get(user_id) == job_id:
                _ACTIVE_JOB_BY_USER.pop(user_id, None)
        raise
    try:
        _ws.track_task(user_id, task)
    except Exception:
        pass
    return {"job_id": job_id, "status": "queued", "reused": False}


async def run_job_from_queue(job_id: str) -> bool:
    """Worker entry: claim a queued job and run the pipeline.

    Called by the internal HTTP hook the events-bridge hits. Returns True when
    the job is finished or gone (bridge should ack), False only when the bridge
    should requeue (claim failed transiently).
    """
    try:
        uuid.UUID(str(job_id))
    except (ValueError, AttributeError, TypeError):
        logger.warning("her_pov worker ignoring malformed id %r", job_id)
        return True  # poison: do not redeliver forever

    user_id: str | None = None
    try:
        from .db import get_conn_autocommit
        async with get_conn_autocommit() as conn:
            row = await (await conn.execute(
                "UPDATE companion_her_pov_jobs "
                "SET status = 'searching', phase = 'searching', "
                "    claimed_at = NOW(), attempts = attempts + 1, "
                "    updated_at = NOW() "
                "WHERE id = %s AND status IN "
                "  ('queued', 'searching', 'thinking', 'drawing') "
                "RETURNING user_id",
                (job_id,),
            )).fetchone()
        if not row:
            # Already terminal or unknown — bridge should ack.
            return True
        user_id = row[0]
    except Exception as e:
        logger.warning("her_pov claim via db failed (%s); trying memory", e)
        async with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if not job:
                return True
            user_id = job.get("user_id")
            if job.get("status") in ("done", "failed"):
                return True

    if not user_id:
        return True

    async with _JOBS_LOCK:
        _ACTIVE_JOB_BY_USER[user_id] = job_id
        job = _JOBS.setdefault(job_id, {"id": job_id, "user_id": user_id})
        job.setdefault("user_id", user_id)
        job["status"] = "searching"
        job["phase"] = "searching"
        job["_touched"] = time.monotonic()

    # Do NOT track on the WS manager: a disconnect must not cancel a durable
    # GPU render that the Commander already paid for with the CTA.
    await run_her_pov(user_id, job_id)
    return True
