"""Tests for db.py — pool init, get_conn retry, health check."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ═══════════════════════════════════════════════════════════════════════════
# get_pool
# ═══════════════════════════════════════════════════════════════════════════


class TestGetPool:
    def test_raises_when_not_initialized(self):
        from app import db
        orig = db._pool
        db._pool = None
        try:
            with pytest.raises(RuntimeError, match="not initialized"):
                db.get_pool()
        finally:
            db._pool = orig

    def test_returns_pool_when_set(self):
        from app import db
        orig = db._pool
        fake = MagicMock()
        db._pool = fake
        try:
            assert db.get_pool() is fake
        finally:
            db._pool = orig


# ═══════════════════════════════════════════════════════════════════════════
# get_conn retry logic
# ═══════════════════════════════════════════════════════════════════════════


class TestGetConnRetry:
    @pytest.mark.asyncio
    async def test_happy_path_no_retry(self):
        from app import db
        fake_pool = MagicMock()
        fake_conn = MagicMock()
        fake_pool.getconn = AsyncMock(return_value=fake_conn)
        fake_pool.putconn = AsyncMock()

        orig = db._pool
        db._pool = fake_pool
        try:
            async with db.get_conn() as c:
                assert c is fake_conn
            fake_pool.putconn.assert_awaited_once_with(fake_conn)
            assert fake_pool.getconn.await_count == 1
        finally:
            db._pool = orig

    @pytest.mark.asyncio
    async def test_retries_on_operational_error(self):
        from app import db
        fake_pool = MagicMock()
        call_count = {"n": 0}

        async def getconn_side_effect():
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise psycopg.OperationalError("connection refused")
            return MagicMock()  # succeeds on 3rd attempt

        fake_pool.getconn = AsyncMock(side_effect=getconn_side_effect)
        fake_pool.putconn = AsyncMock()

        orig = db._pool
        db._pool = fake_pool
        try:
            with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
                async with db.get_conn() as c:
                    assert c is not None
            # MAX_RETRIES + 1 = 3 total attempts
            assert call_count["n"] == 3
        finally:
            db._pool = orig

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self):
        from app import db
        fake_pool = MagicMock()
        fake_pool.getconn = AsyncMock(
            side_effect=psycopg.OperationalError("still down"))
        fake_pool.putconn = AsyncMock()

        orig = db._pool
        db._pool = fake_pool
        try:
            with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
                with pytest.raises(psycopg.OperationalError):
                    async with db.get_conn():
                        pass
        finally:
            db._pool = orig


# ═══════════════════════════════════════════════════════════════════════════
# get_conn_autocommit
# ═══════════════════════════════════════════════════════════════════════════


class TestAutocommit:
    @pytest.mark.asyncio
    async def test_commits_on_clean_exit(self):
        from app import db
        fake_pool = MagicMock()
        fake_conn = MagicMock()
        fake_conn.commit = AsyncMock()
        fake_conn.rollback = AsyncMock()
        fake_pool.getconn = AsyncMock(return_value=fake_conn)
        fake_pool.putconn = AsyncMock()

        orig = db._pool
        db._pool = fake_pool
        try:
            async with db.get_conn_autocommit() as conn:
                pass
            fake_conn.commit.assert_awaited_once()
            fake_conn.rollback.assert_not_called()
        finally:
            db._pool = orig

    @pytest.mark.asyncio
    async def test_rollback_on_exception(self):
        from app import db
        fake_pool = MagicMock()
        fake_conn = MagicMock()
        fake_conn.commit = AsyncMock()
        fake_conn.rollback = AsyncMock()
        fake_pool.getconn = AsyncMock(return_value=fake_conn)
        fake_pool.putconn = AsyncMock()

        orig = db._pool
        db._pool = fake_pool
        try:
            with pytest.raises(ValueError, match="test"):
                async with db.get_conn_autocommit():
                    raise ValueError("test")
            fake_conn.rollback.assert_awaited_once()
            fake_conn.commit.assert_not_called()
        finally:
            db._pool = orig


# ═══════════════════════════════════════════════════════════════════════════
# check_health
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckHealth:
    @pytest.mark.asyncio
    async def test_ok_when_select_succeeds(self):
        from app import db
        fake_pool = MagicMock()
        fake_pool.get_stats = MagicMock(return_value={
            "pool_size": 5, "pool_available": 3, "requests_waiting": 0,
        })
        fake_conn = MagicMock()
        fake_conn.commit = AsyncMock()
        fake_conn.rollback = AsyncMock()
        cur = AsyncMock()
        cur.fetchone = AsyncMock(return_value=(1,))
        fake_conn.execute = AsyncMock(return_value=cur)
        fake_pool.getconn = AsyncMock(return_value=fake_conn)
        fake_pool.putconn = AsyncMock()

        orig = db._pool
        db._pool = fake_pool
        try:
            result = await db.check_health()
            assert result["status"] == "ok"
            assert result["pool_size"] == 5
            assert result["pool_available"] == 3
        finally:
            db._pool = orig

    @pytest.mark.asyncio
    async def test_error_on_exception(self):
        from app import db
        fake_pool = MagicMock()
        fake_pool.getconn = AsyncMock(side_effect=RuntimeError("boom"))

        orig = db._pool
        db._pool = fake_pool
        try:
            with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
                result = await db.check_health()
            assert result["status"] == "error"
            assert "detail" in result
        finally:
            db._pool = orig

    @pytest.mark.asyncio
    async def test_error_when_select_returns_wrong(self):
        from app import db
        fake_pool = MagicMock()
        fake_conn = MagicMock()
        fake_conn.commit = AsyncMock()
        fake_conn.rollback = AsyncMock()
        cur = AsyncMock()
        cur.fetchone = AsyncMock(return_value=(999,))  # Not 1
        fake_conn.execute = AsyncMock(return_value=cur)
        fake_pool.getconn = AsyncMock(return_value=fake_conn)
        fake_pool.putconn = AsyncMock()

        orig = db._pool
        db._pool = fake_pool
        try:
            result = await db.check_health()
            assert result["status"] == "error"
        finally:
            db._pool = orig
