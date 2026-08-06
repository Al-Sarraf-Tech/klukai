"""One-shot deferred tasks — "remind him in three hours".

Cron cannot express this: there is no recurrence, just a single instant in the
future. RabbitMQ can, via TTL + a dead-letter exchange, and no plugin is needed
(`rabbitmq_delayed_message_exchange` is a community `.ez` and is not installed
on this broker).

**Postgres is the source of truth; RabbitMQ is only the timer.** Every task is
written to `companion_scheduled` before anything is published, and delivery
flips its status under a conditional UPDATE. That buys three properties worth
more than the broker round-trip:

- A broker outage *delays* work instead of losing it — the sweeper picks up
  anything overdue.
- A duplicate delivery (the normal at-least-once case) is a no-op, because only
  the first UPDATE matches.
- Scheduling still succeeds when RabbitMQ is unreachable, so nothing on the chat
  path depends on the broker being up.

Delays use fixed **bucket queues** rather than per-message TTL. Per-message TTL
looks simpler and is a trap: RabbitMQ only expires messages at the *head* of a
queue, so one message with a six-hour TTL sitting at the front holds back a
one-minute message queued behind it. Fixed-TTL queues cannot head-of-line block
because every message in them expires in the same order it arrived; longer waits
are built by hopping buckets.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

EXCHANGE_DELAY = "klukai.defer"
EXCHANGE_DUE = "klukai.due"
QUEUE_DUE = "klukai.due.tasks"

# Bucket TTLs in seconds, ascending. A wait is served by repeatedly hopping the
# largest bucket that does not overshoot, so worst-case lateness is one hop of
# the smallest bucket.
DELAY_BUCKETS: tuple[int, ...] = (10, 60, 300, 900, 3600, 21600, 86400)

# Tasks are cheap rows, but a runaway caller should not be able to schedule
# unbounded work.
MAX_DELAY_SECONDS = 30 * 86400
MAX_ATTEMPTS = 5

# How far under a bucket still counts as that bucket. Covers the sub-second
# drift between building a due time and measuring the delay back off it.
BUCKET_TOLERANCE = 1.0


def plan_hops(delay_seconds: float) -> list[int]:
    """Bucket hops that sum to (approximately) ``delay_seconds``.

    Greedy largest-first. The remainder below the smallest bucket is dropped
    rather than rounded up: firing a beat early is kinder than firing late.

    ``BUCKET_TOLERANCE`` matters more than it looks. A caller asking for exactly
    one bucket's worth of delay measures a hair *under* it by the time the
    request is turned into a duration, and a strict comparison then floors the
    whole thing to no hops at all — silently disarming the timer and leaving the
    task to the sweeper. Treat a near-miss as a hit.
    """
    remaining = max(0.0, float(delay_seconds))
    hops: list[int] = []
    for bucket in sorted(DELAY_BUCKETS, reverse=True):
        while remaining >= bucket - BUCKET_TOLERANCE:
            hops.append(bucket)
            remaining -= bucket
            if len(hops) >= 64:  # pathological input guard
                return hops
    return hops


def queue_for(bucket: int) -> str:
    return f"klukai.defer.{bucket}s"


# ── scheduling ──────────────────────────────────────────────────────────────


async def schedule(
    action: dict[str, Any],
    *,
    user_id: str,
    delay_seconds: float | None = None,
    due_at: datetime | None = None,
) -> str | None:
    """Persist a deferred task and arm its timer. Returns the task id.

    Returns None only if the durable write fails — that is the one case where
    the caller genuinely has no task. A broker failure is survivable and does
    not fail the call.
    """
    if due_at is None:
        if delay_seconds is None:
            raise ValueError("schedule() needs either delay_seconds or due_at")
        due_at = datetime.now(timezone.utc) + timedelta(seconds=float(delay_seconds))
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=timezone.utc)

    # Use the caller's requested delay rather than re-deriving it from due_at:
    # measuring the gap back off a timestamp we just built loses microseconds,
    # and that drift is enough to floor an exact-bucket request to no hops.
    now = datetime.now(timezone.utc)
    delay = (
        float(delay_seconds)
        if delay_seconds is not None
        else (due_at - now).total_seconds()
    )
    if delay > MAX_DELAY_SECONDS:
        raise ValueError(f"delay exceeds {MAX_DELAY_SECONDS}s ceiling")

    task_id = str(uuid.uuid4())
    try:
        from .db import get_conn_autocommit

        async with get_conn_autocommit() as conn:
            await conn.execute(
                "INSERT INTO companion_scheduled "
                "(id, user_id, trigger_type, trigger_spec, action, due_at, status) "
                "VALUES (%s, %s, 'once', %s, %s, %s, 'pending')",
                (task_id, user_id, due_at.isoformat(), json.dumps(action), due_at),
            )
    except Exception as e:
        logger.error("Could not persist deferred task: %s", e)
        return None

    # Timer is best-effort — the sweeper is the safety net.
    await _arm_timer(task_id, delay)
    logger.info(
        "Deferred task %s scheduled for %s (in %.0fs) user=%s kind=%s",
        task_id, due_at.isoformat(), delay, user_id, action.get("kind"),
    )
    return task_id


async def _arm_timer(task_id: str, delay_seconds: float) -> bool:
    """Ask the events bridge to arm this task's timer.

    companion-core deliberately speaks no AMQP. The bridge already owns that
    boundary and holds the broker credentials, so core hands it the request over
    the Redis channel it is already subscribed to. That keeps the broker off the
    chat path entirely: if the bridge or RabbitMQ is down, this is a no-op and
    the sweeper delivers the task instead.
    """
    hops = plan_hops(delay_seconds)
    if not hops:
        # Already due (or nearly). Let the sweeper take it on its next pass
        # rather than inventing a zero-length hop.
        return True
    try:
        from . import events

        await events.publish(
            "defer.arm",
            data=task_id,
            domain="defer",
            task_id=task_id,
            hops=hops,
        )
        return True
    except Exception as e:
        logger.warning(
            "Could not arm timer for %s (%s) — sweeper will cover it", task_id, e
        )
        return False


# ── delivery ────────────────────────────────────────────────────────────────


async def claim(task_id: str) -> dict[str, Any] | None:
    """Atomically claim a pending task for delivery.

    The conditional UPDATE is what makes at-least-once delivery safe: a second
    delivery of the same task matches no rows and returns None.
    """
    # `id` is a uuid column, so a malformed id is a type error rather than a
    # miss. Treat it as "no such task" — otherwise the caller sees a DB failure
    # and retries a message that can never succeed.
    try:
        uuid.UUID(str(task_id))
    except (ValueError, AttributeError, TypeError):
        logger.warning("Ignoring deferred task with malformed id %r", task_id)
        return None

    try:
        from .db import get_conn_autocommit

        async with get_conn_autocommit() as conn:
            row = await (await conn.execute(
                "UPDATE companion_scheduled "
                "SET status = 'delivered', delivered_at = NOW(), "
                "    attempts = attempts + 1 "
                "WHERE id = %s AND status = 'pending' "
                "RETURNING user_id, action",
                (task_id,),
            )).fetchone()
        if not row:
            return None
        action = row[1]
        if isinstance(action, str):
            action = json.loads(action)
        return {"task_id": task_id, "user_id": row[0], "action": action or {}}
    except Exception as e:
        logger.error("Could not claim deferred task %s: %s", task_id, e)
        return None


async def fire(task_id: str) -> bool:
    """Claim and execute a deferred task. Idempotent; safe to call twice."""
    claimed = await claim(task_id)
    if not claimed:
        return False
    try:
        await dispatch(claimed["user_id"], claimed["action"])
        return True
    except Exception as e:
        logger.error("Deferred task %s failed: %s", task_id, e, exc_info=True)
        await _mark_failed(task_id, str(e))
        return False


async def _mark_failed(task_id: str, error: str) -> None:
    try:
        from .db import get_conn_autocommit

        async with get_conn_autocommit() as conn:
            # Return it to pending for another try until the ceiling, then park
            # it as failed so it stops being retried forever.
            await conn.execute(
                "UPDATE companion_scheduled SET "
                "status = CASE WHEN attempts >= %s THEN 'failed' ELSE 'pending' END, "
                "last_error = %s "
                "WHERE id = %s",
                (MAX_ATTEMPTS, error[:500], task_id),
            )
    except Exception as e:
        logger.debug("Could not record failure for %s: %s", task_id, e)


async def dispatch(user_id: str, action: dict[str, Any]) -> None:
    """Execute a deferred action.

    Kinds are deliberately narrow — a deferred task carries data, never code,
    so a malformed or stale row can't do anything the app can't already do.
    """
    kind = str(action.get("kind") or "")
    if kind == "message":
        text = str(action.get("text") or "").strip()
        if not text:
            return
        from .context import ws
        await ws.send_proactive(user_id, text)
        return
    if kind == "her_pov":
        from . import memory_her_pov
        await memory_her_pov.start_her_pov(user_id)
        return
    logger.warning("Unknown deferred action kind %r for user %s", kind, user_id)


# ── safety net ──────────────────────────────────────────────────────────────


async def sweep(limit: int = 50) -> int:
    """Fire anything already due that the rail did not deliver.

    This is what makes a broker outage survivable: RabbitMQ going away turns a
    punctual delivery into a slightly late one instead of a lost one.
    """
    try:
        from .db import get_conn

        async with get_conn() as conn:
            rows = await (await conn.execute(
                "SELECT id FROM companion_scheduled "
                "WHERE status = 'pending' AND due_at <= NOW() "
                "ORDER BY due_at ASC LIMIT %s",
                (limit,),
            )).fetchall()
    except Exception as e:
        logger.warning("Deferred sweep query failed: %s", e)
        return 0

    fired = 0
    for (task_id,) in rows:
        if await fire(str(task_id)):
            fired += 1
    if fired:
        logger.info("Deferred sweep fired %d overdue task(s)", fired)
    return fired


async def pending_count(user_id: str | None = None) -> int:
    """How many tasks are still waiting. Used by tests and /api/user/stats."""
    try:
        from .db import get_conn

        async with get_conn() as conn:
            if user_id:
                row = await (await conn.execute(
                    "SELECT COUNT(*) FROM companion_scheduled "
                    "WHERE status = 'pending' AND user_id = %s",
                    (user_id,),
                )).fetchone()
            else:
                row = await (await conn.execute(
                    "SELECT COUNT(*) FROM companion_scheduled WHERE status = 'pending'"
                )).fetchone()
        return int(row[0]) if row else 0
    except Exception as e:
        logger.debug("pending_count failed: %s", e)
        return 0
