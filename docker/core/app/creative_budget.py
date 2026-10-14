"""Persistent, fail-closed attempt quotas for explicitly enabled cloud jobs.

An attempt is spent before dispatch and is never refunded. This bounds retries
and unknown outcomes across workers/restarts; it is not a dollar billing cap.
The ledger belongs on the shared persistent images volume, outside served files.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import closing
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

logger = logging.getLogger(__name__)


class CreativeBudgetError(RuntimeError):
    """No cloud request may be sent when its reservation cannot be persisted."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ledger_path() -> Path:
    path = Path(os.environ.get("KLUKAI_CREATIVE_LEDGER", "/images/.creative/attempts.sqlite3"))
    if not path.is_absolute():
        raise CreativeBudgetError("Creative ledger must be an absolute persistent path")
    return path


def _limits(kind: str) -> tuple[int, int]:
    if kind not in {"image", "text"}:
        raise CreativeBudgetError("Unknown creative request kind")
    try:
        limits = tuple(int(os.environ.get(f"KLUKAI_CLOUD_{kind.upper()}_{period}_LIMIT", "0"))
                       for period in ("DAILY", "MONTHLY"))
    except ValueError:
        raise CreativeBudgetError("Creative limits must be positive integers") from None
    if any(limit <= 0 for limit in limits):
        raise CreativeBudgetError("Cloud creative requests have no enabled allowance")
    return limits[0], limits[1]


def _reserve(kind: str) -> str:
    daily, monthly = _limits(kind)
    path = _ledger_path()
    now = _utc_now().astimezone(timezone.utc)
    day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    attempt_id = str(uuid4())
    conn = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Exclusive create sets permissions without changing a process-wide
        # umask in a multithreaded web worker. SQLite opens the same file below.
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS creative_attempts (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, day TEXT NOT NULL,
            month TEXT NOT NULL, reserved_at TEXT NOT NULL, usage_json TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS creative_attempt_period ON creative_attempts(kind, month, day)")
        month_count, day_count = conn.execute(
            "SELECT count(*), coalesce(sum(day = ?), 0) FROM creative_attempts WHERE kind = ? AND month = ?",
            (day, kind, month),
        ).fetchone()
        if month_count >= monthly or day_count >= daily:
            raise CreativeBudgetError("Cloud creative request allowance exhausted")
        conn.execute(
            "INSERT INTO creative_attempts (id,kind,day,month,reserved_at) VALUES (?,?,?,?,?)",
            (attempt_id, kind, day, month, now.isoformat()),
        )
        conn.execute("COMMIT")
    except (OSError, sqlite3.Error):
        raise CreativeBudgetError("Creative allowance storage unavailable") from None
    finally:
        if conn is not None:
            conn.close()  # Rolls back any incomplete reservation transaction.
    return attempt_id


async def reserve_attempt(kind: str) -> str:
    """Consume one UTC daily/monthly attempt atomically before network I/O."""
    return await asyncio.to_thread(_reserve, kind)


_USAGE_FIELDS = frozenset({
    "input_tokens", "output_tokens", "total_tokens", "text_tokens", "image_tokens",
    "cached_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
    "input_tokens_details", "output_tokens_details",
})


def _numeric_usage(value: dict, depth: int = 0) -> dict:
    result: dict[str, object] = {}
    for key, item in value.items():
        if key not in _USAGE_FIELDS:
            continue
        if type(item) is int and 0 <= item <= 10**12:
            result[key] = item
        elif isinstance(item, dict) and depth < 2:
            result[key] = _numeric_usage(item, depth + 1)
    return result


def _record_usage(attempt_id: str, usage: dict) -> None:
    path = _ledger_path()
    # Do not create a missing ledger merely to record telemetry.
    if not path.is_file():
        return
    with closing(sqlite3.connect(path, timeout=5.0)) as conn, conn:
        conn.execute("UPDATE creative_attempts SET usage_json=? WHERE id=?",
                     (json.dumps(_numeric_usage(usage), separators=(",", ":")), attempt_id))


async def record_usage(attempt_id: str, usage: dict) -> None:
    """Best-effort numeric telemetry; never refund or repeat a dispatched call."""
    try:
        await asyncio.to_thread(_record_usage, attempt_id, usage)
    except (OSError, sqlite3.Error, CreativeBudgetError, TypeError, AttributeError):
        logger.warning("Creative usage recording unavailable")
