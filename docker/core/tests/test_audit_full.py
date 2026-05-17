"""Tests for app.audit — append-only event log + recent() reader.

audit.log() never raises (all DB failures swallowed). audit.recent()
returns [] on failure. Tests cover both paths + chain-hash compute integration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import audit


def _make_pool(prev_hash=None, new_row=(1, datetime(2026, 5, 17, tzinfo=timezone.utc))):
    """Build a mocked pg pool that supports audit.log's call pattern."""
    prev_result = AsyncMock()
    prev_result.fetchone = AsyncMock(return_value=(prev_hash,) if prev_hash else None)

    insert_result = AsyncMock()
    insert_result.fetchone = AsyncMock(return_value=new_row)

    update_result = AsyncMock()
    update_result.fetchone = AsyncMock(return_value=None)

    conn = AsyncMock()
    # Each execute call gets a different mock result; the first is the SELECT,
    # second is the INSERT, third is the UPDATE.
    conn.execute = AsyncMock(side_effect=[prev_result, insert_result, update_result])

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock()

    pool = MagicMock()
    pool.connection = MagicMock(return_value=ctx)
    return pool, conn


class TestEventConstants:
    def test_login_events_present(self):
        assert audit.EVENT_LOGIN_SUCCESS == "login.success"
        assert audit.EVENT_LOGIN_FAILURE == "login.failure"
        assert audit.EVENT_LOGIN_BANNED == "login.banned"

    def test_gameplay_events_present(self):
        assert audit.EVENT_GIFT_GIVEN == "gift.given"
        assert audit.EVENT_MISSION_STARTED == "mission.started"
        assert audit.EVENT_COSTUME_CHANGED == "costume.changed"

    def test_memory_events_present(self):
        assert audit.EVENT_MEMORY_KEPT == "memory.kept"
        assert audit.EVENT_MEMORY_DISCARDED == "memory.discarded"


class TestLog:
    @pytest.mark.asyncio
    async def test_db_failure_swallowed(self):
        with patch("app.audit.get_pool", side_effect=RuntimeError("db down")):
            # Should NOT raise
            await audit.log("login.success", user_id="alice")

    @pytest.mark.asyncio
    async def test_writes_row(self):
        pool, conn = _make_pool()
        with patch("app.audit.get_pool", return_value=pool):
            await audit.log("login.success", user_id="alice", ip_address="1.2.3.4")
        # At least the SELECT (prev_hash) + INSERT happened
        assert conn.execute.call_count >= 2

    @pytest.mark.asyncio
    async def test_writes_chain_hash_via_audit_chain(self):
        pool, conn = _make_pool(prev_hash="abc")
        with patch("app.audit.get_pool", return_value=pool), \
             patch("app.audit_chain.compute_row_hash", return_value="new-hash"):
            await audit.log("gift.given", user_id="alice", metadata={"gift": "flowers"})
        # 3 executes expected: SELECT prev, INSERT, UPDATE chain_hash
        assert conn.execute.call_count == 3

    @pytest.mark.asyncio
    async def test_metadata_serialized_to_json(self):
        pool, conn = _make_pool()
        with patch("app.audit.get_pool", return_value=pool), \
             patch("app.audit_chain.compute_row_hash", return_value="x"):
            await audit.log("login.success", user_id="alice",
                            metadata={"key": "value", "n": 42})
        # The INSERT call should have a stringified JSON in the params
        insert_call = conn.execute.call_args_list[1]
        params = insert_call.args[1]
        # Last param before metadata is request_id; metadata is index 4 in
        # (event_type, user_id, ip_address, request_id, metadata)
        json_str = params[4]
        assert "value" in json_str
        assert "42" in json_str

    @pytest.mark.asyncio
    async def test_chain_hash_failure_swallowed(self):
        pool, conn = _make_pool()
        with patch("app.audit.get_pool", return_value=pool), \
             patch("app.audit_chain.compute_row_hash", side_effect=RuntimeError("hash failed")):
            # Should not raise even if chain_hash compute fails
            await audit.log("login.success", user_id="alice")


class TestRecent:
    @pytest.mark.asyncio
    async def test_returns_empty_on_db_error(self):
        with patch("app.audit.get_pool", side_effect=RuntimeError("db down")):
            result = await audit.recent()
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_formatted_rows(self):
        ts = datetime(2026, 5, 17, tzinfo=timezone.utc)
        rows_result = AsyncMock()
        rows_result.fetchall = AsyncMock(return_value=[
            (1, "login.success", "alice", "1.2.3.4", "req-1", {"k": "v"}, ts),
            (2, "gift.given", "bob", "5.6.7.8", "req-2", None, ts),
        ])

        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=rows_result)

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock()

        pool = MagicMock()
        pool.connection = MagicMock(return_value=ctx)

        with patch("app.audit.get_pool", return_value=pool):
            result = await audit.recent(limit=50)

        assert len(result) == 2
        assert result[0]["event_type"] == "login.success"
        assert result[0]["user_id"] == "alice"
        assert result[1]["event_type"] == "gift.given"

    @pytest.mark.asyncio
    async def test_limit_clamped_to_1000(self):
        ts = datetime(2026, 5, 17, tzinfo=timezone.utc)
        rows_result = AsyncMock()
        rows_result.fetchall = AsyncMock(return_value=[])
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=rows_result)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock()
        pool = MagicMock()
        pool.connection = MagicMock(return_value=ctx)

        with patch("app.audit.get_pool", return_value=pool):
            await audit.recent(limit=99999)

        # Check the actual limit passed to the query
        call_params = conn.execute.call_args.args[1]
        assert call_params[-1] == 1000  # clamped

    @pytest.mark.asyncio
    async def test_limit_clamped_to_1_minimum(self):
        ts = datetime(2026, 5, 17, tzinfo=timezone.utc)
        rows_result = AsyncMock()
        rows_result.fetchall = AsyncMock(return_value=[])
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=rows_result)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock()
        pool = MagicMock()
        pool.connection = MagicMock(return_value=ctx)

        with patch("app.audit.get_pool", return_value=pool):
            await audit.recent(limit=-5)

        call_params = conn.execute.call_args.args[1]
        assert call_params[-1] == 1

    @pytest.mark.asyncio
    async def test_filters_by_event_type(self):
        rows_result = AsyncMock()
        rows_result.fetchall = AsyncMock(return_value=[])
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=rows_result)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock()
        pool = MagicMock()
        pool.connection = MagicMock(return_value=ctx)

        with patch("app.audit.get_pool", return_value=pool):
            await audit.recent(event_type="login.success")

        # SQL should contain WHERE event_type = %s
        sql = conn.execute.call_args.args[0]
        assert "event_type = %s" in sql

    @pytest.mark.asyncio
    async def test_filters_by_user_id(self):
        rows_result = AsyncMock()
        rows_result.fetchall = AsyncMock(return_value=[])
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=rows_result)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock()
        pool = MagicMock()
        pool.connection = MagicMock(return_value=ctx)

        with patch("app.audit.get_pool", return_value=pool):
            await audit.recent(user_id="alice")

        sql = conn.execute.call_args.args[0]
        assert "user_id = %s" in sql
