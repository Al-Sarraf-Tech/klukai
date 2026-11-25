"""Second-pass trust fixes: quota fail-closed, push snapshot, sockets, health."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from app.billing import QuotaExceeded, Subscription, consume_quota
from app.billing_stripe import verify_stripe_signature
from app.routes_health import register_health_routes
from app.ws_manager import WSManager


class _Socket:
    def __init__(self) -> None:
        self.accept = AsyncMock()
        self.send_text = AsyncMock()
        self.close = AsyncMock()


def _frames(socket: _Socket) -> list[dict]:
    return [json.loads(call.args[0]) for call in socket.send_text.await_args_list]


class _Cursor:
    def __init__(self, rows) -> None:
        self.fetchall = AsyncMock(return_value=rows)
        self.fetchone = AsyncMock(return_value=None)


class _Conn:
    def __init__(self, rows) -> None:
        self.rows = rows

    async def execute(self, query, params=None):
        return _Cursor(self.rows)

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


@asynccontextmanager
async def _db(conn: _Conn):
    yield conn


@pytest.mark.asyncio
async def test_quota_write_failure_does_not_admit(monkeypatch):
    monkeypatch.setenv("KLUKAI_PERSONAL_MODE", "false")
    with patch(
        "app.billing.get_subscription",
        new=AsyncMock(return_value=Subscription("alice", "free", "active")),
    ), patch("app.billing.get_conn_autocommit", side_effect=RuntimeError("down")):
        with pytest.raises(QuotaExceeded) as exc:
            await consume_quota("alice", "image_gen_per_day")
    assert exc.value.limit == 3
    with pytest.raises(QuotaExceeded):
        await consume_quota("alice", "image_gen_per_day", amount=-5)


@pytest.mark.asyncio
async def test_stale_push_snapshot_does_not_restore_the_previous_owner():
    from app import push

    push._subscriptions.clear()
    push._initialized = False
    endpoint = "https://push.example/device"
    push._subscriptions["bob"] = [{"endpoint": endpoint, "keys": {"auth": "new"}}]
    conn = _Conn([("alice", {"endpoint": endpoint, "keys": {"auth": "old"}})])
    try:
        with patch("app.db.get_conn_autocommit", lambda: _db(conn)):
            await push.init_subscriptions()
        assert push._subscriptions["bob"][0]["keys"]["auth"] == "new"
        assert push._subscriptions.get("alice", []) == []
    finally:
        push._subscriptions.clear()
        push._initialized = False


@pytest.mark.asyncio
async def test_error_is_not_delivered_when_the_owner_lookup_fails(monkeypatch):
    from app import auth

    manager = WSManager()
    calls = {"n": 0}

    async def owner(session_hash: str, *, refresh: bool = False):
        calls["n"] += 1
        if calls["n"] == 1:
            return "alice"
        if calls["n"] == 2:
            return "bob"
        raise RuntimeError("lookup down")

    monkeypatch.setattr(auth, "get_user_from_session_hash", owner)
    device = _Socket()
    await manager.connect(device, "alice", session_hash="hash-a")
    await manager.send("alice", {"type": "error", "message": "not for bob"})
    assert all(frame.get("type") != "error" for frame in _frames(device))
    assert not manager.has_connection("alice", device)


def _mac(body: bytes, secret: str, ts: int) -> str:
    return hmac.new(
        secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()


def test_non_ascii_stripe_signature_is_rejected_not_raised():
    secret = "whsec_test"
    body = b"{}"
    ts = int(time.time())
    good = _mac(body, secret, ts)
    assert verify_stripe_signature(body, f"t={ts},v1={good},v1=ü", secret) is True
    assert verify_stripe_signature(body, f"t={ts},v1=ü", secret) is False


@pytest.mark.asyncio
async def test_hung_dependency_is_down_not_left_pending(monkeypatch):
    monkeypatch.setattr("app.routes_health._PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "disabled")
    app = FastAPI()

    async def resolve(_request):
        return "alice"

    register_health_routes(app, resolve)
    handler = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/health/subsystems"
    )

    async def hang():
        await asyncio.Event().wait()

    redis = MagicMock()
    redis.ping = hang
    redis.aclose = AsyncMock()
    with patch("app.db.check_health", new=AsyncMock(return_value={"status": "ok"})), patch(
        "redis.asyncio.from_url", return_value=redis
    ), patch("app.grok_chat.configured", return_value=True):
        result = await handler(MagicMock())
    assert result["subsystems"]["redis"]["status"] == "down"
    assert result["status"] != "ok"

    with patch("app.db.check_health", new=hang), patch(
        "redis.asyncio.from_url", return_value=redis
    ), patch("app.grok_chat.configured", return_value=True):
        redis.ping = AsyncMock(return_value=True)
        result = await handler(MagicMock())
    assert result["subsystems"]["database"]["status"] == "down"
    assert result["status"] == "critical"
