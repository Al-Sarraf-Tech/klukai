"""Her POV job rail — the board, its Postgres mirror and the durable queue.

Plumbing for :mod:`.memory_her_pov`, split out so the creative pipeline
(search → compose → render → save) stays in one readable file. Everything
here is about *where a job is and who is running it*: the one-job-per-user
in-process board with its lock and pruning, the best-effort write-through to
``companion_her_pov_jobs``, the stale-row sweep, the events-bridge hand-off,
the inline task spawner and the worker claim.

Module-level state (``_JOBS``, ``_ACTIVE_JOB_BY_USER``, ``_JOBS_LOCK``) has
exactly one home — here. ``memory_her_pov`` re-exports the same objects.
"""

from __future__ import annotations

import asyncio
import logging
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


async def _enqueue_job(job_id: str) -> bool:
    """Ask the events-bridge to park this job on the durable work queue.

    Redis pub/sub does not queue: if the bridge is not subscribed at this
    instant the event is simply gone and the job row would sit at 'queued'
    forever. publish() returns the subscriber count, so a zero here is a
    missed hand-off and the caller runs the job inline instead.
    """
    try:
        from . import events
        received = await events.publish(
            "job.enqueue",
            data=job_id,
            domain="job",
            kind="her_pov",
            job_id=job_id,
        )
        return bool(received)
    except Exception as e:
        logger.warning("her_pov enqueue failed for %s: %s", job_id, e)
        return False


# A job that has not moved in this long is not running anywhere: every phase
# bumps updated_at, and the longest phase (drawing) is bounded by the image
# lock, the LM gate, the lease acquire deadline and the 7-minute render cap.
_STALE_JOB_MINUTES = 20
# Tighter bound for the worker: a bridge redelivery (JOB_RUN_TIMEOUT=900s)
# may only take over an in-progress claim once its heartbeat is this old.
_STALE_CLAIM_MINUTES = 15


async def _claim_active_from_db(user_id: str) -> dict[str, Any] | None:
    """Return an existing non-terminal DB job for this user, if any.

    Before looking, retire rows that stopped moving: a lost hand-off (bridge
    not subscribed when the enqueue was published, core restarted between
    insert and claim) leaves a non-terminal row that nothing will ever
    finish. Without this sweep every later tap returned {"reused": true,
    "status": "queued"} and idx_her_pov_one_active rejected a fresh insert.
    """
    try:
        from .db import get_pool
        get_pool()
    except Exception:
        return None
    try:
        from .db import get_conn, get_conn_autocommit
        async with get_conn_autocommit() as conn:
            stale = await (await conn.execute(
                "UPDATE companion_her_pov_jobs "
                "SET status = 'failed', phase = 'failed', error = 'stale', "
                "    finished_at = NOW(), updated_at = NOW() "
                "WHERE user_id = %s AND status NOT IN ('done', 'failed') "
                "  AND updated_at < NOW() - %s * INTERVAL '1 minute' "
                "RETURNING id",
                (user_id, _STALE_JOB_MINUTES),
            )).fetchall()
        if stale:
            stale_ids = {str(r[0]) for r in stale}
            logger.warning(
                "her_pov retired %d stale job(s) for %s: %s",
                len(stale_ids), user_id, sorted(stale_ids),
            )
            # The in-process claim mirrors the row. In queue mode nothing
            # else clears it until the pipeline runs, so a lost hand-off
            # would still answer "reused" from memory after the DB moved on.
            async with _JOBS_LOCK:
                if _ACTIVE_JOB_BY_USER.get(user_id) in stale_ids:
                    _ACTIVE_JOB_BY_USER.pop(user_id, None)
                for jid in stale_ids:
                    job = _JOBS.get(jid)
                    if job and job.get("status") not in ("done", "failed"):
                        job.update(status="failed", phase="failed", error="stale")
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


async def _start_inline(user_id: str, job_id: str) -> dict[str, Any]:
    """Legacy path: spawn the pipeline as an in-process asyncio task."""
    from .context import ws as _ws
    # The pipeline lives in memory_her_pov (a top-level import would be
    # circular); looked up on the module at call time so tests can patch
    # memory_her_pov.run_her_pov.
    from . import memory_her_pov as _pov
    try:
        task = asyncio.create_task(_pov.run_her_pov(user_id, job_id))
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
            # Take the row only if nobody is working it: 'queued', or an
            # in-progress claim whose heartbeat (updated_at, bumped by every
            # phase) is older than the whole pipeline could legitimately be.
            # A bridge-side timeout requeues the message while the first run
            # is still rendering; re-claiming it bought a second LLM call, a
            # second GPU render and a second archive row.
            row = await (await conn.execute(
                "UPDATE companion_her_pov_jobs "
                "SET status = 'searching', phase = 'searching', "
                "    claimed_at = NOW(), attempts = attempts + 1, "
                "    updated_at = NOW() "
                "WHERE id = %s AND ("
                "  status = 'queued' OR ("
                "    status IN ('searching', 'thinking', 'drawing') "
                "    AND updated_at < NOW() - %s * INTERVAL '1 minute'))"
                "RETURNING user_id",
                (job_id, _STALE_CLAIM_MINUTES),
            )).fetchone()
        if not row:
            # Terminal, unknown, or held by a live claim — bridge should ack.
            # Redelivering a job another run is still finishing would only
            # duplicate the render.
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
    from . import memory_her_pov as _pov  # call-time lookup, see _start_inline
    await _pov.run_her_pov(user_id, job_id)
    return True
