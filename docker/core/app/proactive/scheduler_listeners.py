"""Scheduler failure/redacted audit listeners and successful-fire durability."""

from __future__ import annotations
import asyncio
from collections.abc import Callable, Coroutine
import logging
from typing import Any
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from ..observability.redaction import redact_credentials


def register_listeners(
    scheduler: AsyncIOScheduler,
    spawn: Callable[[Coroutine[Any, Any, None]], asyncio.Task[Any] | None],
    logger: Callable[[], logging.Logger],
) -> None:
    # Surface scheduled-job failures via audit log + structured logger.
    # Without this, an anniversary check (or any other scheduled job) that
    # raises gets swallowed by APScheduler's default handler — the user
    # would silently lose a feature for days.
    try:
        from apscheduler.events import EVENT_JOB_ERROR

        def _on_job_error(event: Any) -> None:
            error = redact_credentials(str(event.exception))
            logger().error(
                "SCHEDULED_JOB_FAILED: job=%s exception=%s traceback=%s",
                event.job_id,
                error,
                redact_credentials(str(event.traceback)),
            )
            # Best-effort audit write — never let it block the listener.
            try:
                from .. import audit

                spawn(
                    audit.log(
                        event_type="scheduled_job.failed",
                        metadata={
                            "job_id": event.job_id,
                            "exception": error[:500],
                        },
                    )
                )
            except Exception:
                pass

        scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)
    except Exception as e:
        logger().warning("Could not wire scheduler error listener: %s", e)

    # Record every successful run so a restart can tell what it missed.
    try:
        from apscheduler.events import EVENT_JOB_EXECUTED

        from .durability import record_fire

        def _on_job_executed(event: Any) -> None:
            try:
                spawn(record_fire(event.job_id))
            except Exception:
                pass

        scheduler.add_listener(_on_job_executed, EVENT_JOB_EXECUTED)
    except Exception as e:
        logger().warning("Could not wire scheduler run recorder: %s", e)
