"""Transactional, checksum-verified startup migrations for PostgreSQL."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

# Stable, database-wide namespace shared by every Klukai startup runner.
_MIGRATION_LOCK_ID = 5425801856799755591


async def apply_migrations(pool: AsyncConnectionPool, migration_dir: Path) -> list[str]:
    """Apply pending SQL atomically, refusing missing or modified applied files.

    Historical pre-ledger schemas are adopted by running the replay-safe SQL
    once. Read all bytes up front so checksums describe exactly what executes.
    Any validation, SQL, or lock error propagates and prevents startup.
    """
    if not migration_dir.is_dir():
        raise RuntimeError(f"Migration directory is missing or invalid: {migration_dir}")
    paths = sorted(migration_dir.glob("*.sql"))
    if not paths:
        raise RuntimeError(f"Migration directory contains no SQL files: {migration_dir}")
    migrations = {}
    for path in paths:
        contents = path.read_bytes()
        migrations[path.name] = (hashlib.sha256(contents).hexdigest(), contents.decode("utf-8"))

    applied = []
    async with pool.connection() as conn:
        async with conn.transaction():
            # SET LOCAL resets at transaction end, including rollback. Bound
            # both startup serialization and schema locks to 30 seconds.
            await conn.execute("SET LOCAL lock_timeout = '30s'")
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_ID,))
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS companion_schema_migrations (
                    name TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cursor = await conn.execute("SELECT name, sha256 FROM companion_schema_migrations")
            recorded: dict[str, str] = dict(await cursor.fetchall())
            # Validate the complete history before running even the first new
            # file (which might sort before the changed or removed file).
            for name, checksum in recorded.items():
                if name not in migrations:
                    raise RuntimeError(f"Applied migration is missing: {name}")
                if migrations[name][0] != checksum:
                    raise RuntimeError(f"Applied migration checksum mismatch: {name}")
            for name, (checksum, statement) in migrations.items():
                if name in recorded:
                    continue
                await conn.execute(statement)
                await conn.execute(
                    "INSERT INTO companion_schema_migrations (name, sha256) VALUES (%s, %s)",
                    (name, checksum),
                )
                applied.append(name)
    # Do not report an applied migration until the enclosing transaction commits.
    for name in applied:
        logger.info("Migration %s applied", name)
    return applied
