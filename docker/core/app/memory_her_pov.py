"""Her POV memory portraits — she picks a moment, journals it, draws it.

User-initiated feature: Commander opens the Her POV menu and asks her to
find *any* real exchange from their history, write a short journal line in
character, and render a picture from her side of the moment. The result is
persisted as a kept Precious Memory tagged ``her_pov``.
"""

from __future__ import annotations

import asyncio
import os
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .her_pov_storage import (
    _load_job_from_db as _load_job_from_db,
    _persist_job as persist_job,
)

from .her_pov_story import (
    _is_trivial as _is_trivial,
    pick_exchange as pick_exchange,
    compose_pov as compose_pov,
)

from .her_pov_pipeline import HerPOVPipeline

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


def _cloud_generation_enabled() -> bool:
    from .creative_text import uses_cloud_text
    from .image_provider import selected_provider, uses_cloud_images

    return uses_cloud_text() or uses_cloud_images() or selected_provider() == "agy"


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


def _prune_jobs_locked(now: float | None = None) -> None:
    """Drop stale jobs. Caller must hold _JOBS_LOCK.

    Entries carry titles, annotations and exchange previews, so an unbounded
    board is a slow leak for the lifetime of the worker.
    """
    now = time.monotonic() if now is None else now
    stale = [
        jid
        for jid, job in _JOBS.items()
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
    await persist_job(
        job_id,
        snapshot,
        strict=_cloud_generation_enabled() or _execution_mode() == "queue",
    )


async def _release_claim(user_id: str, job_id: str) -> None:
    async with _JOBS_LOCK:
        if _ACTIVE_JOB_BY_USER.get(user_id) == job_id:
            _ACTIVE_JOB_BY_USER.pop(user_id, None)


async def run_her_pov(user_id: str, job_id: str) -> None:
    pipeline = HerPOVPipeline(_set_job, pick_exchange, compose_pov, _release_claim)
    await pipeline.run(user_id, job_id)


def _paid_picture_in_flight() -> bool:
    """True when a paid picture is still running, so a stale row must stay.

    Failing it and starting another would buy a second image. The time window
    still unlocks a row once this process no longer holds the picture.
    """
    if not _cloud_generation_enabled():
        return False
    from .account_images import background_render_active
    from .agy_image_provider import generation_in_flight
    from .image_provider import render_lock_held

    return bool(
        generation_in_flight() or background_render_active() or render_lock_held()
    )


def _execution_mode() -> str:
    """inline (default, unit tests) or queue (production durable rail)."""
    return (os.environ.get("HER_POV_EXECUTION") or "inline").strip().lower()


async def _enqueue_job(job_id: str) -> bool:
    """Send a best-effort wake-up hint. Subscriber count is not acceptance.

    The durable row and queued-only reconciler cover missing subscribers and
    crashes/failures anywhere before confirmed AMQP publication.
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
# lock, the LM gate, the lease acquire deadline, one bounded wait for a picture
# already in flight (410 s) and the 380 s render deadline. Twenty minutes is
# past that stack. A paid job is still not failed while a picture is in flight.
_STALE_JOB_MINUTES = 20
# Tighter bound for the worker: a bridge redelivery (JOB_RUN_TIMEOUT=900s)
# may only take over an in-progress claim once its heartbeat is this old.
_STALE_CLAIM_MINUTES = 15


async def _claim_active_from_db(user_id: str) -> dict[str, Any] | None:
    """Return an existing non-terminal DB job for this user, if any.

    A job that has not moved in ``_STALE_JOB_MINUTES`` is marked failed,
    including when the picture is paid, unless a picture is still in flight.
    Failing a live render and starting another would bill twice. Once the
    picture is gone, the old row is failed and the next tap is a new request.
    Queued work is left for the reconciler, at any age.
    """
    try:
        from .db import get_pool

        get_pool()
    except Exception:
        return None
    try:
        from .db import get_conn, get_conn_autocommit

        stale = []
        if _paid_picture_in_flight():
            logger.info("her_pov left the paid job; a picture is still in flight")
        else:
            async with get_conn_autocommit() as conn:
                stale = await (
                    await conn.execute(
                        "UPDATE companion_her_pov_jobs "
                        "SET status = 'failed', phase = 'failed', error = 'stale', "
                        "    finished_at = NOW(), updated_at = NOW() "
                        "WHERE user_id = %s AND status NOT IN ('done', 'failed') "
                        "  AND status <> 'queued' "
                        "  AND updated_at < NOW() - %s * INTERVAL '1 minute' "
                        "RETURNING id",
                        (user_id, _STALE_JOB_MINUTES),
                    )
                ).fetchall()
        if stale:
            stale_ids = {str(r[0]) for r in stale}
            logger.warning(
                "her_pov retired %d stale job(s) for %s: %s",
                len(stale_ids),
                user_id,
                sorted(stale_ids),
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
            row = await (
                await conn.execute(
                    "SELECT id, status FROM companion_her_pov_jobs "
                    "WHERE user_id = %s AND status NOT IN ('done', 'failed') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (user_id,),
                )
            ).fetchone()
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

    Production (`HER_POV_EXECUTION=queue`) commits the durable job row before
    returning acceptance. A Redis hint and queued-only reconciler dispatch its
    UUID to the bridge. Core owns claims and provider/GPU leases. Unit tests
    and local dev keep the inline asyncio path.
    """
    cloud = _cloud_generation_enabled()
    if cloud and _execution_mode() != "queue":
        raise RuntimeError("Cloud memory generation requires the durable job queue")
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
        # Publish the in-process acceptance only after durable insertion.
        # Holding the admission lock makes a concurrent tap wait for that
        # outcome instead of returning a cache-only job that may never exist.
        snapshot = {
            "id": job_id,
            "user_id": user_id,
            "requested_by_user": True,
            "status": "queued",
            "phase": "queued",
            "message": "Standing by…",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        await _persist_job(job_id, snapshot)
        _JOBS[job_id] = {**snapshot, "_touched": time.monotonic()}
        _ACTIVE_JOB_BY_USER[user_id] = job_id
        _prune_jobs_locked()

    mode = _execution_mode()
    if mode == "queue":
        try:
            await _enqueue_job(job_id)
        except Exception:
            logger.warning(
                "her_pov initial dispatch failed; durable job %s remains queued", job_id
            )
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
        cloud = _cloud_generation_enabled()
    except Exception:
        return False
    try:
        from .db import get_conn_autocommit

        async with get_conn_autocommit() as conn:
            # Take the row only if nobody is working it. A paid in-progress
            # job is never reclaimed here: the worker cannot tell whether the
            # first request was billed, and re-running it would buy a second
            # generation. A tap after ``_STALE_JOB_MINUTES`` fails that row
            # and starts a new one. Local work may reclaim a stale claim.
            claim_sql = (
                "UPDATE companion_her_pov_jobs "
                "SET status = 'searching', phase = 'searching', "
                "    claimed_at = NOW(), attempts = attempts + 1, "
                "    updated_at = NOW() "
                "WHERE id = %s AND requested_by_user IS TRUE AND status = 'queued' "
                "AND claimed_at IS NULL AND attempts = 0 AND finished_at IS NULL RETURNING user_id"
            )
            params: tuple[Any, ...] = (job_id,)
            if not cloud:
                claim_sql = (
                    "UPDATE companion_her_pov_jobs "
                    "SET status = 'searching', phase = 'searching', "
                    "    claimed_at = NOW(), attempts = attempts + 1, "
                    "    updated_at = NOW() "
                    "WHERE id = %s AND requested_by_user IS TRUE AND (status = 'queued' "
                    "OR (status IN ('searching', 'thinking', 'drawing') "
                    "AND updated_at < NOW() - %s * INTERVAL '1 minute')) "
                    "RETURNING user_id"
                )
                params = (job_id, _STALE_CLAIM_MINUTES)
            row = await (await conn.execute(claim_sql, params)).fetchone()
        if not row:
            # Terminal, unknown, or held by a live claim — bridge should ack.
            # Redelivering a job another run is still finishing would only
            # duplicate the render.
            return True
        user_id = row[0]
    except Exception as e:
        if cloud:
            logger.warning("her_pov durable claim unavailable; cloud work withheld")
            return False
        logger.warning("her_pov claim via db failed (%s); trying memory", e)
        async with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if not job:
                return True
            if job.get("requested_by_user") is not True:
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
