"""Trust fixes: owner scope, live logout, quotas, health, and rate-limit TTL."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID
import json

import pytest
from fastapi import FastAPI

from app.audit_chain import compute_row_hash, verify_chain
from app.billing import Subscription, consume_quota
from app.billing_stripe import _apply_subscription
from app.message_search import SearchRequest, get_message_context, make_excerpt, search_messages
from app.rate_limit import LIMITS, RateLimitExceeded, check_and_consume
from app.routes_health import register_health_routes
from app.ws_manager import WSManager


class _Socket:
    def __init__(self) -> None:
        self.accept = AsyncMock()
        self.send_text = AsyncMock()
        self.close = AsyncMock()


def _frames(socket: _Socket) -> list[dict]:
    return [json.loads(call.args[0]) for call in socket.send_text.await_args_list]


class _Pipe:
    def __init__(self, results: list) -> None:
        self.results = results

    async def __aenter__(self) -> _Pipe:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def incr(self, key: str) -> _Pipe:
        return self

    def expire(self, key: str, seconds: int, nx: bool = False) -> _Pipe:
        return self

    def ttl(self, key: str) -> _Pipe:
        return self

    def expiretime(self, key: str) -> _Pipe:
        return self

    async def execute(self) -> list:
        return list(self.results)


class _Redis:
    def __init__(self, results: list, *, expire_error: bool = False) -> None:
        self._pipe = _Pipe(results)
        self.armed: list[tuple[str, int]] = []
        self.deleted: list[str] = []
        self.expire_error = expire_error

    def pipeline(self, transaction: bool = True) -> _Pipe:
        return self._pipe

    async def expire(self, key: str, seconds: int) -> bool:
        if self.expire_error:
            raise RuntimeError("expire refused")
        self.armed.append((key, seconds))
        return True

    async def delete(self, key: str) -> int:
        self.deleted.append(key)
        return 1


class _Cursor:
    def __init__(self, *, one=None, many=None) -> None:
        self.fetchone = AsyncMock(return_value=one)
        self.fetchall = AsyncMock(return_value=many or [])


class _Conn:
    def __init__(self, *, one=None, many=None) -> None:
        self.calls: list[tuple] = []
        self._one = one
        self._many = many

    async def execute(self, query, params=None, **kwargs):
        self.calls.append((query, params))
        return _Cursor(one=self._one, many=self._many)

    def transaction(self) -> _Conn:
        return self

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _pool(conn: _Conn):
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool


@asynccontextmanager
async def _db(conn: _Conn):
    yield conn


@pytest.mark.asyncio
async def test_search_keeps_quotes_and_timestamps_inside_the_owner_scope():
    conn = _Conn(many=[])
    when = datetime(2026, 11, 1, tzinfo=timezone.utc)
    request = SearchRequest(
        query="O'Reilly",
        start=when,
        cursor={"created_at": "2026-11-01T00:00:00+00:00", "id": str(UUID(int=2))},
    )
    with patch("app.message_search.get_pool", return_value=_pool(conn)):
        result = await search_messages("alice", request)
    assert result["messages"] == []
    query, params = next(call for call in conn.calls if not isinstance(call[0], str))
    text = query.as_string(None)
    assert "companion_conversations" in text
    assert "O'Reilly" not in text
    assert params[0] == "O'Reilly"
    assert params[1] == "alice" and params[2] == "alice"
    assert "O'Reilly" in params[3]
    assert params[4] == when
    assert make_excerpt("it's fine", None) == "it's fine"


@pytest.mark.asyncio
async def test_context_anchor_is_scoped_to_the_caller_conversation():
    conn = _Conn(one=None)
    with patch("app.message_search.get_pool", return_value=_pool(conn)):
        assert await get_message_context(
            "alice",
            type("Req", (), {"anchor_id": UUID(int=3), "before": None, "after": None})(),
        ) is None
    _query, params = conn.calls[-1]
    assert params == ("alice", UUID(int=3), "alice")


def test_broken_audit_chain_does_not_verify():
    row = {
        "id": 1,
        "event_type": "login.success",
        "user_id": "alice",
        "ip_address": None,
        "request_id": None,
        "metadata": {"username": "alice"},
        "created_at": "2026-11-01T00:00:00+00:00",
    }
    row["chain_hash"] = compute_row_hash(
        row_id=1,
        event_type=row["event_type"],
        user_id="alice",
        ip_address=None,
        request_id=None,
        metadata=row["metadata"],
        created_at=row["created_at"],
        prev_hash=None,
    )
    tampered = dict(row)
    tampered["event_type"] = "login.failure"
    assert verify_chain([tampered])["valid"] is False
    tampered["chain_hash"] = None
    broken = verify_chain([row, tampered])
    assert broken["valid"] is False
    assert broken["break_at_id"] == 1


@pytest.mark.asyncio
async def test_logout_closes_only_that_sessions_socket(monkeypatch):
    from app import auth, context

    manager = WSManager()
    monkeypatch.setattr(context, "ws", manager)

    owners = {
        auth._hash_token("token-a"): "alice",
        auth._hash_token("token-b"): "alice",
        auth._hash_token("token-c"): "bob",
    }

    async def owner(session_hash: str, *, refresh: bool = False) -> str | None:
        return owners.get(session_hash)

    monkeypatch.setattr(auth, "get_user_from_session_hash", owner)
    this_device, other_device, bob = _Socket(), _Socket(), _Socket()
    await manager.connect(
        this_device, "alice", session_hash=auth._hash_token("token-a")
    )
    await manager.connect(
        other_device, "alice", session_hash=auth._hash_token("token-b")
    )
    await manager.connect(bob, "bob", session_hash=auth._hash_token("token-c"))

    conn = _Conn(one=("alice",))
    with patch("app.auth.get_conn_autocommit", lambda: _db(conn)):
        assert await auth.logout("token-a") == "alice"

    assert not manager.has_connection("alice", this_device)
    assert manager.has_connection("alice", other_device)
    assert manager.has_connection("bob", bob)
    assert this_device.close.await_args.kwargs["code"] == 4001
    other_device.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_error_reaches_the_submitting_device_but_not_a_foreign_session(monkeypatch):
    from app import auth

    manager = WSManager()
    state = {"owner": "alice"}

    async def owner(session_hash: str, *, refresh: bool = False):
        return state["owner"]

    monkeypatch.setattr(auth, "get_user_from_session_hash", owner)
    device = _Socket()
    await manager.connect(device, "alice", session_hash="hash-a")
    await manager.send("alice", {"type": "error", "message": "try again"})
    assert _frames(device)[-1] == {"type": "error", "message": "try again"}

    state["owner"] = None
    await manager.send("alice", {"type": "error", "message": "still yours"})
    assert _frames(device)[-1] == {"type": "error", "message": "still yours"}
    assert not manager.has_connection("alice", device)

    foreign = _Socket()
    state["owner"] = "alice"
    await manager.connect(foreign, "alice", session_hash="hash-b")
    state["owner"] = "bob"
    await manager.send("alice", {"type": "error", "message": "not for bob"})
    assert all(frame.get("type") != "error" for frame in _frames(foreign))
    assert not manager.has_connection("alice", foreign)


@pytest.mark.asyncio
async def test_ttl_less_counter_is_armed_instead_of_locking_forever():
    redis = _Redis(
        [LIMITS["login"].requests + 1, False, -1, -1],
    )
    with patch("app.rate_limit._get_redis", new=AsyncMock(return_value=redis)):
        with pytest.raises(RateLimitExceeded) as exc:
            await check_and_consume("alice", "login")
    assert exc.value.retry_after == LIMITS["login"].window_seconds
    assert redis.armed and redis.armed[0][1] == LIMITS["login"].window_seconds
    assert redis.deleted == []

    stuck = _Redis([LIMITS["login"].requests + 5, False, -1, -1], expire_error=True)
    with patch("app.rate_limit._get_redis", new=AsyncMock(return_value=stuck)):
        with pytest.raises(RateLimitExceeded):
            await check_and_consume("alice", "login")
    assert stuck.deleted


@pytest.mark.asyncio
async def test_subscription_without_entitlement_does_not_activate(monkeypatch):
    monkeypatch.setenv("STRIPE_PRICE_PRO_MONTHLY", "price_pro")
    monkeypatch.setenv("KLUKAI_PERSONAL_MODE", "false")
    conn = _Conn()
    event = {
        "data": {
            "object": {
                "id": "sub_1",
                "customer": "cus_1",
                "metadata": {"user_id": "alice"},
                "items": {"data": [{"price": {"id": "price_pro"}}]},
            }
        }
    }
    with patch("app.billing_stripe.get_conn_autocommit", lambda: _db(conn)):
        result = await _apply_subscription(event)
    assert result["tier"] == "free"
    assert result["status"] == "canceled"
    assert conn.calls[0][1][1] == "free"

    quota = _Conn(one=(4,))
    with patch(
        "app.billing.get_subscription",
        new=AsyncMock(return_value=Subscription("alice", "pro", "canceled")),
    ), patch("app.billing.get_conn_autocommit", lambda: _db(quota)):
        from app.billing import QuotaExceeded

        with pytest.raises(QuotaExceeded) as exc:
            await consume_quota("alice", "image_gen_per_day")
    assert exc.value.tier == "free"
    assert exc.value.limit == 3


@pytest.mark.asyncio
async def test_database_error_is_critical_and_false_redis_ping_is_down():
    app = FastAPI()

    async def resolve(_request):
        return "alice"

    register_health_routes(app, resolve)
    handler = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/health/subsystems"
    )
    redis = MagicMock()
    redis.ping = AsyncMock(return_value=False)
    redis.aclose = AsyncMock()
    with patch(
        "app.db.check_health",
        new=AsyncMock(return_value={"status": "error", "detail": "OperationalError"}),
    ), patch("redis.asyncio.from_url", return_value=redis), patch(
        "app.grok_chat.configured", return_value=True
    ):
        result = await handler(MagicMock())
    assert result["subsystems"]["database"]["status"] == "down"
    assert result["subsystems"]["database"]["detail"] == "OperationalError"
    assert result["status"] == "critical"
    redis.aclose.assert_awaited()


@pytest.mark.asyncio
async def test_push_endpoint_moves_to_the_new_user():
    from app import push

    push._subscriptions.clear()
    conn = _Conn()
    with patch("app.db.get_conn_autocommit", lambda: _db(conn)):
        await push.add_subscription("alice", {"endpoint": "https://push.example/device"})
        await push.add_subscription(
            "bob", {"endpoint": "https://push.example/device", "keys": {"auth": "new"}}
        )
    assert push._subscriptions["alice"] == []
    assert push._subscriptions["bob"][0]["keys"]["auth"] == "new"
    assert "user_id = EXCLUDED.user_id" in conn.calls[-1][0]
    push._subscriptions.clear()
