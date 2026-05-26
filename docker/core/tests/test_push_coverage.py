"""Behavioral coverage top-up for app.push — send_push fan-out + retirement.

Existing tests cover get_vapid_public_key / add_subscription / remove_subscription
(memory side) / init_subscriptions. This file targets the remaining surface:

  * send_push (lines 119-165): VAPID gate, per-user vs broadcast targeting,
    per-device webpush dispatch, failure → endpoint retirement, sent count.
  * remove_subscription DB paths (98-99, 104-107): the inner async _delete and
    both the running-loop (create_task) and no-loop (run_until_complete) branches.

webpush is patched (pywebpush is installed in the venv); no real network, no DB.
State is reset around every test so the in-memory subscription cache can't leak.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import push


@pytest.fixture(autouse=True)
def _reset_state():
    push._subscriptions.clear()
    push._initialized = False
    yield
    push._subscriptions.clear()
    push._initialized = False


# ═══════════════════════════════════════════════════════════════════════════
# send_push — VAPID gate / targeting / dispatch / retirement (119-165)
# ═══════════════════════════════════════════════════════════════════════════
class TestSendPush:
    @pytest.mark.asyncio
    async def test_returns_zero_when_no_vapid_key(self):
        """No private key configured → send nothing, return 0 (and never import
        pywebpush)."""
        push._initialized = True  # skip init_subscriptions DB work
        push._subscriptions["alice"] = [{"endpoint": "https://a/p"}]
        with patch.object(push, "VAPID_PRIVATE_KEY", ""):
            sent = await push.send_push("Hi", "body", user_id="alice")
        assert sent == 0

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_targets(self):
        """VAPID configured but the user has no subscriptions → 0, webpush unused."""
        push._initialized = True
        wp = MagicMock()
        with patch.object(push, "VAPID_PRIVATE_KEY", "priv"), \
             patch("pywebpush.webpush", wp):
            sent = await push.send_push("Hi", "body", user_id="nobody")
        assert sent == 0
        wp.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_to_single_user_only(self):
        """user_id given → only that user's devices receive the push."""
        push._initialized = True
        push._subscriptions["alice"] = [
            {"endpoint": "https://a/1"},
            {"endpoint": "https://a/2"},
        ]
        push._subscriptions["bob"] = [{"endpoint": "https://b/1"}]
        wp = MagicMock()
        with patch.object(push, "VAPID_PRIVATE_KEY", "priv"), \
             patch("pywebpush.webpush", wp):
            sent = await push.send_push("T", "B", user_id="alice")

        assert sent == 2
        # Exactly alice's two endpoints, never bob's.
        endpoints = {c.kwargs["subscription_info"]["endpoint"] for c in wp.call_args_list}
        assert endpoints == {"https://a/1", "https://a/2"}

    @pytest.mark.asyncio
    async def test_broadcasts_to_all_users_when_no_user_id(self):
        """No user_id → every subscription across all users is targeted."""
        push._initialized = True
        push._subscriptions["alice"] = [{"endpoint": "https://a/1"}]
        push._subscriptions["bob"] = [{"endpoint": "https://b/1"}]
        wp = MagicMock()
        with patch.object(push, "VAPID_PRIVATE_KEY", "priv"), \
             patch("pywebpush.webpush", wp):
            sent = await push.send_push("T", "B")

        assert sent == 2
        endpoints = {c.kwargs["subscription_info"]["endpoint"] for c in wp.call_args_list}
        assert endpoints == {"https://a/1", "https://b/1"}

    @pytest.mark.asyncio
    async def test_payload_truncates_body_and_carries_data(self):
        """Body is clipped to 200 chars and data dict is forwarded in the JSON."""
        import json
        push._initialized = True
        push._subscriptions["alice"] = [{"endpoint": "https://a/1"}]
        wp = MagicMock()
        long_body = "x" * 500
        with patch.object(push, "VAPID_PRIVATE_KEY", "priv"), \
             patch("pywebpush.webpush", wp):
            await push.send_push("Title", long_body, data={"url": "/chat"}, user_id="alice")

        payload = json.loads(wp.call_args.kwargs["data"])
        assert payload["title"] == "Title"
        assert len(payload["body"]) == 200
        assert payload["data"] == {"url": "/chat"}
        # VAPID material is passed through to the dispatcher
        assert wp.call_args.kwargs["vapid_private_key"] == "priv"
        assert wp.call_args.kwargs["vapid_claims"] == push.VAPID_CLAIMS

    @pytest.mark.asyncio
    async def test_failed_endpoint_is_retired(self):
        """A device whose webpush raises is counted as failed and removed via
        remove_subscription; the healthy device still counts as sent."""
        push._initialized = True
        push._subscriptions["alice"] = [
            {"endpoint": "https://good/1"},
            {"endpoint": "https://dead/1"},
        ]

        def _wp(subscription_info, **kw):
            if subscription_info["endpoint"] == "https://dead/1":
                raise RuntimeError("410 Gone")

        removed: list[str] = []
        with patch.object(push, "VAPID_PRIVATE_KEY", "priv"), \
             patch("pywebpush.webpush", side_effect=_wp), \
             patch.object(push, "remove_subscription", side_effect=lambda ep: removed.append(ep)):
            sent = await push.send_push("T", "B", user_id="alice")

        assert sent == 1, "only the healthy device counts"
        assert removed == ["https://dead/1"], "dead endpoint retired exactly once"

    @pytest.mark.asyncio
    async def test_returns_zero_when_pywebpush_missing(self):
        """If pywebpush can't be imported, send_push logs and returns 0 rather
        than raising (the ImportError branch, lines 136-138)."""
        push._initialized = True
        push._subscriptions["alice"] = [{"endpoint": "https://a/1"}]

        real_import = __import__

        def _fake_import(name, *args, **kwargs):
            if name == "pywebpush":
                raise ImportError("no pywebpush")
            return real_import(name, *args, **kwargs)

        with patch.object(push, "VAPID_PRIVATE_KEY", "priv"), \
             patch("builtins.__import__", side_effect=_fake_import):
            sent = await push.send_push("T", "B", user_id="alice")
        assert sent == 0

    @pytest.mark.asyncio
    async def test_calls_init_subscriptions_first(self):
        """send_push always primes the cache via init_subscriptions()."""
        push._initialized = False
        called = {"init": False}

        async def _fake_init():
            called["init"] = True
            push._initialized = True

        with patch.object(push, "init_subscriptions", side_effect=_fake_init), \
             patch.object(push, "VAPID_PRIVATE_KEY", ""):
            await push.send_push("T", "B", user_id="alice")
        assert called["init"] is True


# ═══════════════════════════════════════════════════════════════════════════
# remove_subscription — DB delete coroutine, both loop branches (98-107)
# ═══════════════════════════════════════════════════════════════════════════
class TestRemoveSubscriptionDB:
    def test_running_loop_schedules_delete_task(self):
        """Inside a running event loop, the DB delete is scheduled as a task
        (create_task branch). We assert create_task was invoked with a coroutine
        and that memory was pruned."""
        push._subscriptions["alice"] = [{"endpoint": "https://x/p"}]

        loop = MagicMock()
        loop.is_running.return_value = True
        captured = {}

        def _create_task(coro):
            captured["coro"] = coro
            coro.close()  # avoid 'never awaited' warning — we only assert scheduling
            return MagicMock()

        with patch("asyncio.get_event_loop", return_value=loop), \
             patch("asyncio.create_task", side_effect=_create_task):
            push.remove_subscription("https://x/p")

        assert push._subscriptions["alice"] == []  # memory pruned (line 91)
        assert asyncio.iscoroutine(captured["coro"])  # the _delete() coroutine was built

    def test_no_running_loop_runs_delete_to_completion(self):
        """Outside a running loop, _delete is driven via run_until_complete and
        actually issues the DELETE against the (mocked) pool."""
        push._subscriptions["alice"] = [{"endpoint": "https://x/p"}]

        executed: list[tuple] = []
        conn = MagicMock()

        async def _execute(sql, params=None):
            executed.append((sql, params))

        conn.execute = _execute
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=None)

        real_loop = asyncio.new_event_loop()
        loop = MagicMock()
        loop.is_running.return_value = False
        loop.run_until_complete.side_effect = lambda coro: real_loop.run_until_complete(coro)

        try:
            with patch("asyncio.get_event_loop", return_value=loop), \
                 patch("app.db.get_conn_autocommit", return_value=ctx):
                push.remove_subscription("https://x/p")
        finally:
            real_loop.close()

        assert push._subscriptions["alice"] == []
        assert any("DELETE FROM companion_push_subscriptions" in s for (s, _) in executed)
        assert executed and executed[0][1] == ("https://x/p",)

    def test_db_exception_is_swallowed(self):
        """If scheduling/DB raises, memory is still pruned and no error escapes
        (the outer try/except pass, lines 108-109)."""
        push._subscriptions["alice"] = [{"endpoint": "https://x/p"}]
        with patch("asyncio.get_event_loop", side_effect=RuntimeError("no loop")):
            push.remove_subscription("https://x/p")  # must not raise
        assert push._subscriptions["alice"] == []
