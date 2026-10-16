"""Authoritative proactive job registrations, with local-time cron injection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from apscheduler.triggers.cron import CronTrigger
from .state import QUIET_HOUR_END


@dataclass(frozen=True)
class ScheduleSpec:
    method: str
    job_id: str
    cron_fields: dict[str, int | str] | None
    interval_minutes: int | None = None


SCHEDULE = (
    ScheduleSpec("_morning_checkin", "morning_checkin", {"hour": 8, "minute": 0}),
    ScheduleSpec("_daily_challenge", "daily_challenge", {"hour": 9, "minute": 0}),
    ScheduleSpec(
        "_promise_followup_check", "promise_followup", {"hour": "10,14,18", "minute": 0}
    ),
    ScheduleSpec("_evening_checkin", "evening_checkin", {"hour": 22, "minute": 0}),
    ScheduleSpec("_idle_check", "idle_check", {"hour": "9-21/2", "minute": 30}),
    ScheduleSpec(
        "_unsent_message_check",
        "unsent_message_check",
        {"hour": "10-22/3", "minute": 15},
    ),
    ScheduleSpec("_mission_report", "mission_report", {"hour": 14, "minute": 45}),
    ScheduleSpec("_random_event", "random_event", {"hour": "9-22", "minute": "15,45"}),
    ScheduleSpec("_mission_random_event", "mission_random_event", None, 5),
    ScheduleSpec("_dream_event", "dream_event", {"hour": "1-4", "minute": 37}),
    ScheduleSpec(
        "_memory_recall_tick", "memory_recall", {"hour": "11,17", "minute": 20}
    ),
    ScheduleSpec(
        "_spontaneous_art_tick", "spontaneous_art", {"hour": "15,19", "minute": 40}
    ),
    ScheduleSpec("_daily_recap", "daily_recap", {"hour": 21, "minute": 0}),
    ScheduleSpec("_romance_window", "romance_window", {"hour": 20, "minute": 30}),
    ScheduleSpec("_reset_daily", "daily_reset", {"hour": 0, "minute": 0}),
    ScheduleSpec(
        "_weekly_reflection",
        "weekly_reflection",
        {"day_of_week": "sun", "hour": 21, "minute": 0},
    ),
    ScheduleSpec(
        "_anniversary_check", "anniversary_check", {"hour": QUIET_HOUR_END, "minute": 0}
    ),
    ScheduleSpec("_seasonal_check", "seasonal_check", {"hour": 9, "minute": 0}),
    ScheduleSpec("_quiet_day_check", "quiet_day_check", {"hour": 15, "minute": 10}),
    ScheduleSpec(
        "_command_decision_check", "command_decision", {"hour": 14, "minute": 40}
    ),
    ScheduleSpec(
        "_portrait_backfill", "portrait_backfill", {"hour": "2-6", "minute": "*/15"}
    ),
    ScheduleSpec("_deferred_sweep", "deferred_sweep", None, 1),
)


def register_schedule(
    add_job: Callable[..., object],
    callbacks: Mapping[str, Callable[[], Awaitable[None]]],
    cron: Callable[..., CronTrigger],
) -> None:
    for spec in SCHEDULE:
        callback = callbacks[spec.method]
        if spec.cron_fields is not None:
            add_job(
                callback,
                cron(**spec.cron_fields),
                id=spec.job_id,
                replace_existing=True,
            )
        else:
            add_job(
                callback,
                "interval",
                minutes=spec.interval_minutes,
                id=spec.job_id,
                replace_existing=True,
            )
