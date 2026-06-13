"""Coverage tests for events.py (Redis pub/sub) and push.py (subscription mgmt)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ═══════════════════════════════════════════════════════════════════════════
# events.py — publish/init/close
# ═══════════════════════════════════════════════════════════════════════════


class TestEventsPublish:
    def setup_method(self):
        import app.events as events_mod
        events_mod._redis = None

    @pytest.mark.asyncio
    async def test_publish_is_noop_when_redis_not_initialized(self):
        """publish() should return silently if init() was never called."""
        from app.events import publish
        await publish("test.event")  # must not raise

    @pytest.mark.asyncio
    async def test_publish_sends_json_to_channel(self):
        from app import events
        fake = AsyncMock()
        fake.publish = AsyncMock(return_value=1)
        events._redis = fake

        await events.publish("greeting", data="hello", user_id="alice")

        fake.publish.assert_awaited_once()
        channel, payload = fake.publish.call_args[0]
        assert channel == events.CHANNEL
        import json
        parsed = json.loads(payload)
        assert parsed["type"] == "greeting"
        assert parsed["data"] == "hello"
        assert parsed["user_id"] == "alice"

    @pytest.mark.asyncio
    async def test_publish_swallows_redis_errors(self):
        """Publish failures must not propagate — just log."""
        from app import events
        fake = AsyncMock()

        async def broken(*_a, **_kw):
            raise RuntimeError("redis broken")

        fake.publish = broken
        events._redis = fake

        await events.publish("something")  # should not raise

    @pytest.mark.asyncio
    async def test_init_sets_global_redis(self):
        from app import events

        # Fully mock the Redis factory
        fake_redis = AsyncMock()
        with patch("app.events.aioredis.from_url", return_value=fake_redis):
            await events.init()

        assert events._redis is fake_redis

    @pytest.mark.asyncio
    async def test_close_calls_aclose_and_clears(self):
        from app import events
        fake = AsyncMock()
        fake.aclose = AsyncMock()
        events._redis = fake

        await events.close()

        fake.aclose.assert_awaited_once()
        assert events._redis is None

    @pytest.mark.asyncio
    async def test_close_noop_when_no_redis(self):
        from app import events
        events._redis = None
        await events.close()  # should not raise
        assert events._redis is None


# ═══════════════════════════════════════════════════════════════════════════
# push.py — subscription CRUD + VAPID
# ═══════════════════════════════════════════════════════════════════════════


class _FakeConn:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.executed = []

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        result = AsyncMock()
        result.fetchall = AsyncMock(return_value=self._rows)
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class TestVapidKey:
    def test_returns_env_var(self):
        from app import push
        # get_vapid_public_key just returns the module-level VAPID_PUBLIC_KEY
        k = push.get_vapid_public_key()
        assert k == push.VAPID_PUBLIC_KEY  # whatever the env var says


class TestAddSubscription:
    def setup_method(self):
        from app import push
        push._subscriptions.clear()

    @pytest.mark.asyncio
    async def test_empty_endpoint_noop(self):
        from app.push import add_subscription, _subscriptions
        await add_subscription("alice", {"endpoint": ""})
        assert "alice" not in _subscriptions

    @pytest.mark.asyncio
    async def test_new_subscription_added_to_memory_and_db(self):
        from app import push
        conn = _FakeConn()

        with patch("app.db.get_conn_autocommit", return_value=conn):
            await push.add_subscription("alice", {
                "endpoint": "https://example.com/push/abc",
                "keys": {"p256dh": "x", "auth": "y"},
            })

        assert "alice" in push._subscriptions
        assert len(push._subscriptions["alice"]) == 1
        # DB write attempted
        inserts = [s for s, _ in conn.executed if "INSERT" in s]
        assert len(inserts) == 1

    @pytest.mark.asyncio
    async def test_duplicate_endpoint_not_added_twice_in_memory(self):
        from app import push
        conn = _FakeConn()
        sub = {"endpoint": "https://example.com/push/abc", "keys": {}}

        with patch("app.db.get_conn_autocommit", return_value=conn):
            await push.add_subscription("alice", sub)
            await push.add_subscription("alice", sub)

        assert len(push._subscriptions["alice"]) == 1

    @pytest.mark.asyncio
    async def test_multiple_endpoints_per_user(self):
        from app import push
        conn = _FakeConn()
        with patch("app.db.get_conn_autocommit", return_value=conn):
            await push.add_subscription("alice", {"endpoint": "https://e/1"})
            await push.add_subscription("alice", {"endpoint": "https://e/2"})

        assert len(push._subscriptions["alice"]) == 2

    @pytest.mark.asyncio
    async def test_db_failure_swallowed(self):
        """Memory add should persist even if DB write raises."""
        from app import push

        def broken():
            raise RuntimeError("db down")

        with patch("app.db.get_conn_autocommit", side_effect=broken):
            await push.add_subscription("alice", {"endpoint": "https://e/x"})

        assert len(push._subscriptions["alice"]) == 1


class TestRemoveSubscription:
    def setup_method(self):
        from app import push
        push._subscriptions.clear()

    def test_remove_drops_from_memory(self):
        from app import push
        push._subscriptions["alice"] = [
            {"endpoint": "https://e/1"},
            {"endpoint": "https://e/2"},
        ]
        push.remove_subscription("https://e/1")
        assert len(push._subscriptions["alice"]) == 1
        assert push._subscriptions["alice"][0]["endpoint"] == "https://e/2"

    def test_remove_unknown_endpoint_noop(self):
        from app import push
        push._subscriptions["alice"] = [{"endpoint": "https://e/1"}]
        push.remove_subscription("https://nope/x")
        assert len(push._subscriptions["alice"]) == 1


class TestInitSubscriptions:
    def setup_method(self):
        import app.push as push_mod
        push_mod._subscriptions.clear()
        push_mod._initialized = False

    @pytest.mark.asyncio
    async def test_loads_from_db(self):
        from app import push
        rows = [("alice", {"endpoint": "https://e/1"}),
                ("bob", {"endpoint": "https://e/2"})]
        conn = _FakeConn(rows=rows)

        with patch("app.db.get_conn_autocommit", return_value=conn):
            await push.init_subscriptions()

        assert "alice" in push._subscriptions
        assert "bob" in push._subscriptions
        assert push._initialized is True

    @pytest.mark.asyncio
    async def test_init_idempotent(self):
        from app import push
        push._initialized = True
        conn = _FakeConn(rows=[("alice", {"endpoint": "e/1"})])
        with patch("app.db.get_conn_autocommit", return_value=conn):
            await push.init_subscriptions()  # should NOT re-query
        # No subscriptions loaded because init was skipped
        assert "alice" not in push._subscriptions

    @pytest.mark.asyncio
    async def test_handles_json_string_column(self):
        """subscription column may come back as JSON string if driver differs."""
        from app import push
        import json as _json
        rows = [("alice", _json.dumps({"endpoint": "https://e/s"}))]
        conn = _FakeConn(rows=rows)
        with patch("app.db.get_conn_autocommit", return_value=conn):
            await push.init_subscriptions()
        assert len(push._subscriptions["alice"]) == 1

    @pytest.mark.asyncio
    async def test_db_failure_still_marks_initialized(self):
        from app import push

        def broken():
            raise RuntimeError("db down")

        with patch("app.db.get_conn_autocommit", side_effect=broken):
            await push.init_subscriptions()

        # Still flipped so we don't retry on every call
        assert push._initialized is True
