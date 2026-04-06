"""Shared async connection pool for PostgreSQL with retry and health check."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import psycopg
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://aichat:aichat@aichat-db:5432/aichat"
)

_pool: AsyncConnectionPool | None = None

# Retry config
MAX_RETRIES = 2
RETRY_DELAY = 0.5  # seconds


async def init_pool(min_size: int = 2, max_size: int = 10) -> None:
    """Create and open the shared connection pool."""
    global _pool
    _pool = AsyncConnectionPool(
        conninfo=DATABASE_URL,
        min_size=min_size,
        max_size=max_size,
        open=False,
        check=AsyncConnectionPool.check_connection,
        max_idle=300.0,
        reconnect_timeout=30.0,
    )
    await _pool.open()
    logger.info("Database pool opened (min=%d, max=%d)", min_size, max_size)


async def close_pool() -> None:
    """Close the shared connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Database pool closed")


def get_pool() -> AsyncConnectionPool:
    """Get the shared pool. Raises if not initialized."""
    if _pool is None:
        raise RuntimeError("Database pool not initialized — call init_pool() first")
    return _pool


@asynccontextmanager
async def get_conn() -> AsyncIterator[psycopg.AsyncConnection]:
    """Get a connection with automatic retry on stale/broken connections.

    Usage:
        async with get_conn() as conn:
            await conn.execute("SELECT 1")
            await conn.commit()
    """
    pool = get_pool()
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            async with pool.connection() as conn:
                yield conn
                return
        except (psycopg.OperationalError, psycopg.InterfaceError) as e:
            last_err = e
            if attempt < MAX_RETRIES:
                logger.warning("DB connection error (attempt %d/%d): %s — retrying",
                               attempt + 1, MAX_RETRIES + 1, e)
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
            else:
                logger.error("DB connection failed after %d attempts: %s", MAX_RETRIES + 1, e)
                raise
    raise last_err  # should not reach here


@asynccontextmanager
async def get_conn_autocommit() -> AsyncIterator[psycopg.AsyncConnection]:
    """Get a connection that auto-commits on successful exit.

    Usage:
        async with get_conn_autocommit() as conn:
            await conn.execute("INSERT INTO ...")
            # commit happens automatically on clean exit
    """
    async with get_conn() as conn:
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def check_health() -> dict:
    """Check database connectivity. Returns status dict for health endpoint."""
    try:
        async with get_conn() as conn:
            row = await (await conn.execute("SELECT 1")).fetchone()
            if row and row[0] == 1:
                # Also check pool stats
                pool = get_pool()
                stats = pool.get_stats()
                return {
                    "status": "ok",
                    "pool_size": stats.get("pool_size", 0),
                    "pool_available": stats.get("pool_available", 0),
                    "requests_waiting": stats.get("requests_waiting", 0),
                }
        return {"status": "error", "detail": "SELECT 1 failed"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
