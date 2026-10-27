"""Klukai's factual memory, read and written directly in her own database.

The rows live in the ``memory`` key/value table of the database core already
uses (``DATABASE_URL``). Until 2026-10 core reached them through the aichat
stack's data service (``aichat-data`` ``/memory/store`` and ``/memory/recall``);
these queries are the same ones that service ran, so the existing rows are
read and written in place and nothing is migrated. Nothing here deletes a row:
an expired entry is only hidden, exactly as the data service hid it.
"""

from __future__ import annotations

from typing import Any

from .db import get_conn, get_conn_autocommit

# The data service's pattern recall returned at most this many rows, newest
# first. Callers (dossier, inside jokes, milestones) rely on the same bound.
PATTERN_LIMIT = 50


# The table predates Klukai's migration ledger (aichat-data created it) and
# already exists in production. A fresh database gets the identical schema on
# first use. This is deliberately not a migration: an applied migration file
# that an older image lacks would stop that image from starting, and an
# automatic rollback must always be able to start the previous release.
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS memory ("
    "key TEXT PRIMARY KEY, value TEXT NOT NULL, category TEXT DEFAULT '', "
    "ts TIMESTAMPTZ NOT NULL DEFAULT NOW(), expires_at TIMESTAMPTZ)",
    "CREATE INDEX IF NOT EXISTS idx_memory_category ON memory (category) "
    "WHERE category <> ''",
    "CREATE INDEX IF NOT EXISTS idx_memory_expires ON memory (expires_at) "
    "WHERE expires_at IS NOT NULL",
)
_ready = False


async def _ensure_table() -> None:
    """Idempotent; checked once per process, every statement IF NOT EXISTS."""
    global _ready
    if _ready:
        return
    async with get_conn_autocommit() as conn:
        found = await (
            await conn.execute("SELECT to_regclass('public.memory') IS NOT NULL")
        ).fetchone()
        if not (found and found[0]):
            for statement in _SCHEMA:
                await conn.execute(statement)
    _ready = True


def _entry(row: tuple[Any, ...]) -> dict[str, Any]:
    return {"key": row[0], "value": row[1], "ts": str(row[2])}


async def store(key: str, value: str, ttl_seconds: int | None = None) -> None:
    """Insert or replace one fact. A positive TTL makes it expire; storing
    without one makes it permanent again."""
    key = key.strip()
    if not key:
        raise ValueError("key must not be empty")
    await _ensure_table()
    async with get_conn_autocommit() as conn:
        if ttl_seconds and ttl_seconds > 0:
            seconds = max(1, int(ttl_seconds))
            await conn.execute(
                "INSERT INTO memory(key, value, ts, expires_at) "
                "VALUES(%s, %s, NOW(), NOW() + make_interval(secs => %s)) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, ts=NOW(), "
                "expires_at=NOW() + make_interval(secs => %s)",
                (key, value, seconds, seconds),
            )
        else:
            await conn.execute(
                "INSERT INTO memory(key, value, ts) VALUES(%s, %s, NOW()) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, ts=NOW(), "
                "expires_at=NULL",
                (key, value),
            )


async def recall(key: str) -> dict[str, Any] | None:
    """The live entry stored under exactly ``key``, or None."""
    await _ensure_table()
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT key, value, ts FROM memory "
            "WHERE key=%s AND (expires_at IS NULL OR expires_at > NOW())",
            (key.strip(),),
        )
        row = await cur.fetchone()
    return _entry(row) if row else None


async def recall_pattern(pattern: str) -> list[dict[str, Any]]:
    """Live entries whose key matches the SQL LIKE ``pattern``, newest first."""
    await _ensure_table()
    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT key, value, ts FROM memory "
            "WHERE key LIKE %s AND (expires_at IS NULL OR expires_at > NOW()) "
            "ORDER BY ts DESC LIMIT %s",
            (pattern.strip(), PATTERN_LIMIT),
        )
        rows = await cur.fetchall()
    return [_entry(row) for row in rows]
