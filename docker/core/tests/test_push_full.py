"""Tests for app.push — VAPID push subscription management."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app import push


def _mk_pool_ctx(fetchall_result=None):
    """Build a mocked get_conn_autocommit context manager."""
    conn = AsyncMock()
    if fetchall_result is not None:
        result = AsyncMock()
        result.fetchall = AsyncMock(return_value=fetchall_result)
        conn.execute = AsyncMock(return_value=result)
    else:
        conn.execute = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock()
    return ctx, conn


@pytest.fixture(autouse=True)
def _reset_state():
    push._subscriptions.clear()
    push._initialized = False
    yield
    push._subscriptions.clear()
    push._initialized = False


class TestGetVapidPublicKey:
    def test_returns_env_value(self):
        # Just checks the function returns a string (may be empty in dev)
        result = push.get_vapid_public_key()
        assert isinstance(result, str)


class TestAddSubscription:
    @pytest.mark.asyncio
    async def test_adds_to_memory(self):
        ctx, conn = _mk_pool_ctx()
        with patch("app.db.get_conn_autocommit", return_value=ctx):
            await push.add_subscription("alice", {"endpoint": "https://x/push", "keys": {}})
        assert len(push._subscriptions["alice"]) == 1

    @pytest.mark.asyncio
    async def test_empty_endpoint_silently_ignored(self):
        await push.add_subscription("alice", {})
        assert "alice" not in push._subscriptions

    @pytest.mark.asyncio
    async def test_duplicate_endpoint_not_added_twice(self):
        ctx, _ = _mk_pool_ctx()
        with patch("app.db.get_conn_autocommit", return_value=ctx):
            await push.add_subscription("alice", {"endpoint": "https://x/p"})
            await push.add_subscription("alice", {"endpoint": "https://x/p"})
        assert len(push._subscriptions["alice"]) == 1

    @pytest.mark.asyncio
    async def test_different_endpoints_both_added(self):
        ctx, _ = _mk_pool_ctx()
        with patch("app.db.get_conn_autocommit", return_value=ctx):
            await push.add_subscription("alice", {"endpoint": "https://a/p"})
            await push.add_subscription("alice", {"endpoint": "https://b/p"})
        assert len(push._subscriptions["alice"]) == 2

    @pytest.mark.asyncio
    async def test_separate_users_isolated(self):
        ctx, _ = _mk_pool_ctx()
        with patch("app.db.get_conn_autocommit", return_value=ctx):
            await push.add_subscription("alice", {"endpoint": "https://a/p"})
            await push.add_subscription("bob", {"endpoint": "https://b/p"})
        assert len(push._subscriptions["alice"]) == 1
        assert len(push._subscriptions["bob"]) == 1

    @pytest.mark.asyncio
    async def test_db_error_does_not_block_memory_add(self):
        with patch("app.db.get_conn_autocommit", side_effect=RuntimeError("db down")):
            await push.add_subscription("alice", {"endpoint": "https://x/p"})
        # Memory still updated even with DB failure
        assert "alice" in push._subscriptions
        assert len(push._subscriptions["alice"]) == 1


class TestRemoveSubscription:
    def test_removes_from_memory(self):
        push._subscriptions["alice"] = [{"endpoint": "https://x/p"}]
        with patch("app.db.get_conn_autocommit"):
            push.remove_subscription("https://x/p")
        # Memory side-effect happens regardless of DB
        assert push._subscriptions["alice"] == []

    def test_removes_only_matching_endpoint(self):
        push._subscriptions["alice"] = [
            {"endpoint": "https://a/p"},
            {"endpoint": "https://b/p"},
        ]
        with patch("app.db.get_conn_autocommit"):
            push.remove_subscription("https://a/p")
        assert len(push._subscriptions["alice"]) == 1
        assert push._subscriptions["alice"][0]["endpoint"] == "https://b/p"

    def test_unknown_endpoint_noop(self):
        push._subscriptions["alice"] = [{"endpoint": "https://x/p"}]
        with patch("app.db.get_conn_autocommit"):
            push.remove_subscription("https://nonexistent/p")
        assert len(push._subscriptions["alice"]) == 1


class TestInitSubscriptions:
    @pytest.mark.asyncio
    async def test_idempotent_after_first_call(self):
        push._initialized = True
        # Second call short-circuits — no DB access attempted
        with patch("app.db.get_conn_autocommit") as gc:
            await push.init_subscriptions()
            gc.assert_not_called()

    @pytest.mark.asyncio
    async def test_loads_subscriptions_from_db(self):
        rows = [
            ("alice", {"endpoint": "https://a/p", "keys": {}}),
            ("alice", {"endpoint": "https://b/p", "keys": {}}),
            ("bob",   {"endpoint": "https://c/p", "keys": {}}),
        ]
        ctx, _ = _mk_pool_ctx(fetchall_result=rows)
        with patch("app.db.get_conn_autocommit", return_value=ctx):
            await push.init_subscriptions()
        assert len(push._subscriptions["alice"]) == 2
        assert len(push._subscriptions["bob"]) == 1

    @pytest.mark.asyncio
    async def test_db_error_marks_initialized_to_skip_retries(self):
        with patch("app.db.get_conn_autocommit", side_effect=RuntimeError("db down")):
            await push.init_subscriptions()
        assert push._initialized is True
