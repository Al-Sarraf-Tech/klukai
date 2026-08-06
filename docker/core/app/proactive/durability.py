"""Make the recurring scheduler survive restarts.

APScheduler's jobs live in a ``MemoryJobStore`` with a one-second
``misfire_grace_time``. That combination loses work in two different ways, and
neither leaves a trace:

- A fire time that passes **while the process is down** is not "missed" from
  APScheduler's point of view — the job did not exist yet, so nothing is logged
  and nothing runs. A deploy at 07:58 silently costs the 08:00 morning check-in.
- A fire time that passes while the event loop is busy for more than one second
  is discarded as a misfire.

This module records when each job actually ran, and on startup replays the ones
that should have fired while she was down.

Replay is a **curated allowlist**, not every job. Re-running the midnight
counter reset at 09:00, or firing an hour-old "random lore event", is worse than
skipping it. Only jobs whose value survives being late are listed, each with a
ceiling past which being late stops being useful — a three-day-old morning
briefing is noise, not care.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# A briefly busy event loop must not silently drop a scheduled job. Five minutes
# is long enough to ride out image generation or a cold model load, short enough
# that a fire is still recognisably "on time".
MISFIRE_GRACE_SECONDS = 300

# job_id -> how stale a missed fire may be before replaying it stops being kind.
CATCH_UP_WINDOWS: dict[str, timedelta] = {
    # Greetings and summaries: better late than never, within the same day.
    "morning_checkin": timedelta(hours=4),
    "daily_challenge": timedelta(hours=4),
    "evening_checkin": timedelta(hours=3),
    "daily_recap": timedelta(hours=4),
    # Dated occasions: missing one entirely is the bad outcome, so allow longer.
    "anniversary_check": timedelta(hours=14),
    "seasonal_check": timedelta(hours=14),
    "weekly_reflection": timedelta(hours=36),
    # Follow-ups the Commander is owed.
    "promise_followup": timedelta(hours=5),
    "unsent_message_check": timedelta(hours=3),
    # Pattern-gated and self-suppressing, so a late run is harmless.
    "quiet_day_check": timedelta(hours=3),
}

# Deliberately NOT replayed, and why — kept explicit so nobody "fixes" it later:
#   daily_reset          replaying at 09:00 would wipe counters the day has
#                        already accrued; a missed reset self-corrects at the
#                        next midnight.
#   random_event         ambience. A late one is noise, not a missed message.
#   mission_random_event interval job, only meaningful while a mission is live.
#   idle_check           asks "is he idle *right now*" — inherently not replayable.
#   dream_event          only coherent inside its 1-4am window.
#   memory_recall        random-rolled ambience; it will roll again.
#   spontaneous_art      ditto, and it burns a GPU render.
#   romance_window       tied to a specific time of evening.
#   mission_report       superseded by the next day's report.
#   deferred_sweep       runs every minute anyway; replaying it is pointless.
NEVER_CATCH_UP = frozenset({
    "daily_reset", "random_event", "mission_random_event", "idle_check",
    "dream_event", "memory_recall", "spontaneous_art", "romance_window",
    "mission_report", "deferred_sweep",
})

# Guard against a pathological trigger walking forever.
_MAX_TRIGGER_STEPS = 2000


async def record_fire(job_id: str, status: str = "ok") -> None:
    """Note that ``job_id`` just ran. Fail-soft: bookkeeping never breaks a job."""
    try:
        from ..db import get_conn_autocommit

        async with get_conn_autocommit() as conn:
            await conn.execute(
                "INSERT INTO companion_job_runs (job_id, last_fired, last_status) "
                "VALUES (%s, NOW(), %s) "
                "ON CONFLICT (job_id) DO UPDATE SET "
                "last_fired = NOW(), last_status = %s, updated_at = NOW()",
                (job_id, status, status),
            )
    except Exception as e:
        logger.debug("Could not record fire for %s: %s", job_id, e)


async def load_last_fired() -> dict[str, datetime]:
    """Map job_id -> when it last ran. Empty on any failure (nothing replays)."""
    try:
        from ..db import get_conn

        async with get_conn() as conn:
            rows = await (await conn.execute(
                "SELECT job_id, last_fired FROM companion_job_runs"
            )).fetchall()
        out: dict[str, datetime] = {}
        for job_id, last_fired in rows:
            if last_fired is None:
                continue
            if last_fired.tzinfo is None:
                last_fired = last_fired.replace(tzinfo=timezone.utc)
            out[str(job_id)] = last_fired
        return out
    except Exception as e:
        logger.warning("Could not load job run history: %s", e)
        return {}


def previous_fire_time(
    trigger: Any, now: datetime, earliest: datetime
) -> datetime | None:
    """Most recent time ``trigger`` should have fired, within [earliest, now].

    APScheduler triggers only walk forwards, so step from ``earliest`` and keep
    the last fire time that is still in the past. Returns None if the trigger
    was never due in that window.
    """
    try:
        candidate = trigger.get_next_fire_time(None, earliest)
    except Exception:
        return None

    latest: datetime | None = None
    steps = 0
    while candidate is not None and candidate <= now:
        latest = candidate
        steps += 1
        if steps >= _MAX_TRIGGER_STEPS:
            logger.warning("Trigger walk hit the step ceiling; using %s", latest)
            break
        try:
            nxt = trigger.get_next_fire_time(candidate, candidate)
        except Exception:
            break
        # A trigger that stops advancing would spin forever.
        if nxt is None or nxt <= candidate:
            break
        candidate = nxt
    return latest


def missed_jobs(
    jobs: list[tuple[str, Any]],
    last_fired: dict[str, datetime],
    now: datetime | None = None,
) -> list[tuple[str, datetime]]:
    """Which allowlisted jobs should have fired while she was down.

    A job is missed when its most recent scheduled fire time is inside its
    catch-up window and is *newer* than the last run we recorded. A job with no
    recorded history is treated as missed only if it was due inside the window —
    so a fresh database does not replay a day of greetings at once.
    """
    now = now or datetime.now(timezone.utc)
    out: list[tuple[str, datetime]] = []

    for job_id, trigger in jobs:
        window = CATCH_UP_WINDOWS.get(job_id)
        if window is None:
            continue  # not allowlisted (or explicitly never replayed)
        earliest = now - window
        due = previous_fire_time(trigger, now, earliest)
        if due is None:
            continue
        seen = last_fired.get(job_id)
        if seen is not None and seen >= due:
            continue  # already ran for that fire time
        out.append((job_id, due))

    out.sort(key=lambda pair: pair[1])
    return out


async def run_catch_up(
    jobs: list[tuple[str, Any, Callable[[], Awaitable[None]]]],
    now: datetime | None = None,
) -> list[str]:
    """Replay jobs missed while the process was down. Returns the job ids run.

    Every failure is contained: one job raising must not stop the rest, and the
    whole pass must never prevent the scheduler from starting.
    """
    try:
        last_fired = await load_last_fired()
        pending = missed_jobs([(jid, trig) for jid, trig, _ in jobs], last_fired, now)
        if not pending:
            logger.info("Scheduler catch-up: nothing missed")
            return []

        funcs = {jid: fn for jid, _, fn in jobs}
        ran: list[str] = []
        for job_id, due in pending:
            fn = funcs.get(job_id)
            if fn is None:
                continue
            logger.info(
                "Scheduler catch-up: replaying %s (was due %s)",
                job_id, due.isoformat(),
            )
            try:
                await fn()
                await record_fire(job_id, status="caught_up")
                ran.append(job_id)
            except Exception as e:
                logger.error("Catch-up for %s failed: %s", job_id, e, exc_info=True)
                await record_fire(job_id, status="error")
        return ran
    except Exception as e:
        logger.error("Scheduler catch-up pass failed: %s", e, exc_info=True)
        return []
