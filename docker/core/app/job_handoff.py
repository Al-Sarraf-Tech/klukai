"""Republish unstarted durable jobs; Redis is a wake-up hint, never a receipt."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import logging

logger = logging.getLogger(__name__)
POLL_SECONDS = 5.0
RETRY_SECONDS = 30
BATCH_SIZE = 16


async def reconcile_once(job_id: str | None = None, *, batch_size: int = BATCH_SIZE) -> int:
    """Commit bounded dispatch marks before publishing, safe across replicas.

    No job is created or claimed here. Crash after the commit or a Redis/AMQP
    failure costs at most one retry interval; repeated hints retain the UUID.
    Started/ambiguous work is deliberately excluded, even if marked queued.
    """
    from .db import get_conn_autocommit
    from .memory_her_pov import _enqueue_job

    async with get_conn_autocommit() as conn:
        rows = await (await conn.execute(
            "WITH pending AS ("
            " SELECT id FROM companion_her_pov_jobs"
            " WHERE status = 'queued' AND claimed_at IS NULL AND attempts = 0"
            " AND finished_at IS NULL"
            " AND (last_enqueued_at IS NULL OR last_enqueued_at < NOW() - %s * INTERVAL '1 second')"
            " AND (%s::uuid IS NULL OR id = %s::uuid)"
            " ORDER BY last_enqueued_at NULLS FIRST, created_at, id"
            " LIMIT %s FOR UPDATE SKIP LOCKED"
            ") UPDATE companion_her_pov_jobs j SET last_enqueued_at = NOW()"
            " FROM pending WHERE j.id = pending.id RETURNING j.id",
            (RETRY_SECONDS, job_id, job_id, max(1, min(batch_size, BATCH_SIZE))),
        )).fetchall()
    # Transaction commits on context exit, before any lossy wake-up is sent.
    for row in rows:
        await _enqueue_job(str(row[0]))
    return len(rows)


async def _reconcile_safely() -> None:
    try:
        await reconcile_once()
    except Exception:
        logger.warning("Her POV dispatch reconciliation failed; will retry", exc_info=True)


async def _loop() -> None:
    while True:
        await asyncio.sleep(POLL_SECONDS)
        await _reconcile_safely()


@asynccontextmanager
async def running():
    """Startup recovery plus owned, cancellable reconciliation in queue mode."""
    from .memory_her_pov import _execution_mode

    if _execution_mode() != "queue":
        yield
        return
    await _reconcile_safely()
    task = asyncio.create_task(_loop(), name="her-pov-handoff")
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
