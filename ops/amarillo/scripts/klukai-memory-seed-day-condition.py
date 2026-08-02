#!/usr/bin/env python3
"""Allow memory seeding only on an even local epoch day inside 03:00-06:00."""

from __future__ import annotations

from datetime import date, datetime
import sys
from zoneinfo import ZoneInfo


LOCAL_ZONE = ZoneInfo("America/Chicago")
EPOCH_DATE = date(1970, 1, 1)
WINDOW_START_HOUR = 3
WINDOW_END_HOUR = 6


def should_run(moment: datetime) -> bool:
    """Return true on one deterministic parity of local calendar dates."""
    local = moment.astimezone(LOCAL_ZONE)
    epoch_day = (local.date() - EPOCH_DATE).days
    return (
        epoch_day % 2 == 0
        and WINDOW_START_HOUR <= local.hour < WINDOW_END_HOUR
    )


def main() -> int:
    return 0 if should_run(datetime.now(LOCAL_ZONE)) else 1


if __name__ == "__main__":
    sys.exit(main())
