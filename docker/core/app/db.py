"""Shared async connection pool for PostgreSQL."""

from __future__ import annotations

import logging
import os

from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://aichat:aichat@aichat-db:5432/aichat"
)

_pool: AsyncConnectionPool | None = None


async def init_pool(min_size: int = 2, max_size: int = 10) -> None:
    """Create and open the shared connection pool."""
    global _pool
    _pool = AsyncConnectionPool(
        conninfo=DATABASE_URL,
        min_size=min_size,
        max_size=max_size,
        open=False,
        # Validate connections before handing them out — catches stale/closed connections
        check=AsyncConnectionPool.check_connection,
        # Recycle connections older than 5 minutes to prevent stale state
        max_idle=300.0,
        # Reconnect attempts
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
