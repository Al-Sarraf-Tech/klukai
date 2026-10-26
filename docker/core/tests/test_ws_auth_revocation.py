"""Live WebSocket credentials must not outlive their database session."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bcrypt
import pytest
from fastapi import FastAPI, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app import auth, chat, context, db, routes_extras3
from app.models import SessionState
from app.ws_manager import WSManager


class AuthDatabase:
    """Offline auth boundary with actual expiry, deletion and account state."""
    def __init__(self):
        self.users = {"alice": True, "bob": True}
        self.password = bcrypt.hashpw(b"old-password", bcrypt.gensalt(rounds=4)).decode()
        now = datetime.now(timezone.utc)
        self.sessions = {
            auth._hash_token(token): (user, now + timedelta(days=5), now)
            for token, user in [("alice-one", "alice"), ("alice-two", "alice"), ("bob-one", "bob")]
        }
        self.statements = []

    @asynccontextmanager
    async def connection(self):
        yield self

    async def execute(self, sql, params=None):
        self.statements.append((sql, params))
        row = None
        if sql.startswith("SELECT s.user_id"):
            row = self.sessions.get(params[0])
            if row and not self.users[row[0]] and "u.deactivated_at IS NULL" in sql:
                row = None
        elif sql.startswith("SELECT password_hash"):
            row = (self.password,)
        elif sql.startswith("UPDATE companion_users SET password_hash"):
            if len(params) < 3 or self.password == params[2]:
                self.password = params[0]
                row = (params[1],)
        elif sql.startswith("UPDATE companion_users SET deactivated_at"):
            self.users[params[0]] = False
        elif sql.startswith("DELETE FROM companion_auth_sessions WHERE user_id"):
            self.sessions = {key: value for key, value in self.sessions.items() if value[0] != params[0]}
        return SimpleNamespace(fetchone=AsyncMock(return_value=row))


class Device:
    application_state = client_state = WebSocketState.CONNECTED

    def __init__(self, token="alice-one"):
        self.query_params = {"token": token}
        self.frames = asyncio.Queue()
        self.receiving = asyncio.Event()
        self.output = []
        self.accept = AsyncMock()
        self.close = AsyncMock()

    async def send_text(self, payload):
        self.output.append(json.loads(payload))

    async def receive_text(self):
        self.receiving.set()
        frame = await self.frames.get()
        if frame is None:
            raise WebSocketDisconnect()
        return json.dumps(frame)


@pytest.fixture
async def setup(monkeypatch):
    database = AuthDatabase()
    manager = WSManager()
    monkeypatch.setattr(auth, "get_conn_autocommit", database.connection)
    monkeypatch.setattr(db, "get_conn_autocommit", database.connection)
    monkeypatch.setattr(context, "ws", manager)
    monkeypatch.setattr(chat, "ws", manager)
    monkeypatch.setattr(chat, "_initialize_session", AsyncMock())
    monkeypatch.setattr(chat, "_maybe_reflect_on_return", AsyncMock())
    monkeypatch.setattr(chat, "_maybe_oath_on_connect", AsyncMock())
    monkeypatch.setattr("app.rituals.on_connect", AsyncMock())
    monkeypatch.setattr("app.wardrobe.warm_today", AsyncMock())
    monkeypatch.setattr("app.audit.log", AsyncMock())
    app = FastAPI()
    chat.register_websocket(app)
    endpoint = next(route.endpoint for route in app.routes if route.path == "/ws")
    tasks = []

    async def start(token="alice-one"):
        device = Device(token)
        task = asyncio.create_task(endpoint(device))
        tasks.append(task)
        await asyncio.wait_for(device.receiving.wait(), 1)
        return device, task

    yield SimpleNamespace(database=database, manager=manager, start=start)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for user in tuple(manager._connections):
        await manager.disconnect(user)


def invalidate(database, reason, token="alice-one"):
    key = auth._hash_token(token)
    user, expires, created = database.sessions[key]
    if reason == "revoked":
        del database.sessions[key]
    elif reason == "expired":
        database.sessions[key] = (user, datetime.now(timezone.utc) - timedelta(seconds=1), created)
    elif reason == "absolute_lifetime":
        database.sessions[key] = (user, expires, datetime.now(timezone.utc) - timedelta(days=31))
    else:
        database.users[user] = False


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["revoked", "expired", "absolute_lifetime", "deactivated"])
async def test_passive_broadcast_rejects_invalid_session(setup, reason):
    device, _ = await setup.start()
    other, _ = await setup.start("bob-one")
    invalidate(setup.database, reason)
    await setup.manager.send("alice", {"type": "token", "text": "private"})
    assert device.output == [{"type": "connected", "status": "ok"}]
    assert not setup.manager.has_connection("alice", device)
    assert setup.manager.has_connection("bob", other)
    assert device.close.await_args.kwargs["code"] == 4001


@pytest.mark.asyncio
async def test_only_expired_device_is_retired(setup):
    expired, _ = await setup.start()
    valid, _ = await setup.start("alice-two")
    invalidate(setup.database, "expired")
    await setup.manager.send("alice", {"type": "token", "text": "private"})
    assert len(expired.output) == 1
    assert valid.output[-1] == {"type": "token", "text": "private"}
    assert setup.manager.has_connection("alice", valid)


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [
    {"type": "message", "content": "stale"},
    {"type": "voice_end", "audio": "stale"},
    {"type": "tap_interact"},
])
async def test_inbound_frame_revalidates_before_dispatch(setup, monkeypatch, frame):
    admissions = []

    async def admit(*args):
        admissions.append(args)
        raise WebSocketDisconnect()

    monkeypatch.setattr(chat, "_admit_turn", admit)
    monkeypatch.setattr(chat, "_handle_tap_interact", admit)
    device, task = await setup.start()
    invalidate(setup.database, "revoked")
    await device.frames.put(frame)
    await asyncio.wait_for(task, 1)
    assert admissions == []
    assert device.close.await_args.kwargs["code"] == 4001


@pytest.mark.asyncio
async def test_queued_turn_revalidates_after_acquiring_owner(setup, monkeypatch):
    device, _ = await setup.start()
    locked, release = asyncio.Event(), asyncio.Event()
    saved = []
    handled = []

    @asynccontextmanager
    async def owner(_):
        locked.set()
        await release.wait()
        yield

    async def save(*args):
        saved.append(args)

    async def handle(*args):
        handled.append(args)

    memory = SimpleNamespace(session_owner=owner, get_session=AsyncMock(return_value=SessionState(conversation_id="test")), save_session=save)
    monkeypatch.setattr(chat, "memory", memory)
    monkeypatch.setattr(chat, "_handle_message", handle)
    turn = asyncio.create_task(chat._admit_turn("message", "queued", "alice", device))
    await asyncio.wait_for(locked.wait(), 1)
    invalidate(setup.database, "revoked")
    release.set()
    await asyncio.wait_for(turn, 1)
    assert saved == []
    assert handled == []
    assert not setup.manager.has_connection("alice", device)


@pytest.mark.asyncio
async def test_password_change_closes_all_user_devices_immediately(setup):
    first, first_task = await setup.start()
    second, second_task = await setup.start("alice-two")
    bob, _ = await setup.start("bob-one")
    assert await auth.change_password("alice", "old-password", "new-password")
    assert not setup.manager.is_connected("alice")
    assert setup.manager.has_connection("bob", bob)
    await asyncio.wait_for(asyncio.gather(first_task, second_task), 1)
    assert first.close.await_args.kwargs["code"] == 4001
    assert second.close.await_args.kwargs["code"] == 4001


@pytest.mark.asyncio
async def test_deactivation_closes_passive_socket_immediately(setup, monkeypatch):
    device, task = await setup.start()
    monkeypatch.setattr(routes_extras3, "_get_user_id", AsyncMock(return_value="alice"))
    app = FastAPI()
    routes_extras3.register_extras3(app)
    route = next(route.endpoint for route in app.routes if route.path == "/api/account/deactivate")
    result = await route(SimpleNamespace(), SimpleNamespace())
    assert result["deactivated"]
    assert not setup.manager.is_connected("alice")
    await asyncio.wait_for(task, 1)
    assert device.close.await_args.kwargs["code"] == 4001


@pytest.mark.asyncio
async def test_passive_validation_uses_hash_without_refreshing_expiry(setup):
    device, _ = await setup.start()
    key = auth._hash_token("alice-one")
    user, _, created = setup.database.sessions[key]
    setup.database.sessions[key] = (user, datetime.now(timezone.utc) + timedelta(hours=1), created)
    setup.database.statements.clear()
    await setup.manager.send("alice", {"type": "mood", "mood": "composed"})
    assert device.output[-1]["type"] == "mood"
    assert setup.database.statements
    assert all(sql.startswith("SELECT ") for sql, _ in setup.database.statements)
    assert all(params == (key,) for _, params in setup.database.statements)
    assert setup.manager._session_hashes[device] == key
    assert "alice-one" not in setup.manager._session_hashes.values()


@pytest.mark.asyncio
async def test_auth_database_failure_fails_closed(setup, monkeypatch):
    device, _ = await setup.start()
    monkeypatch.setattr(setup.database, "execute", AsyncMock(side_effect=RuntimeError("offline")))
    await setup.manager.send("alice", {"type": "token", "text": "private"})
    assert len(device.output) == 1
    assert not setup.manager.has_connection("alice", device)
    assert device.close.await_args.kwargs["code"] == 4001


@pytest.mark.asyncio
async def test_different_session_owner_fails_closed(setup):
    device, _ = await setup.start()
    key = auth._hash_token("alice-one")
    _, expires, created = setup.database.sessions[key]
    setup.database.sessions[key] = ("bob", expires, created)
    await setup.manager.send("alice", {"type": "token", "text": "private"})
    assert len(device.output) == 1
    assert not setup.manager.has_connection("alice", device)


@pytest.mark.asyncio
async def test_revocation_during_auth_lookup_does_not_resurrect_membership(setup, monkeypatch):
    device, _ = await setup.start()
    entered, release = asyncio.Event(), asyncio.Event()

    async def lookup(*_, **__):
        entered.set()
        await release.wait()
        return "alice"

    monkeypatch.setattr(auth, "get_user_from_session_hash", lookup)
    sending = asyncio.create_task(setup.manager.send("alice", {"type": "token", "text": "private"}))
    await asyncio.wait_for(entered.wait(), 1)
    await setup.manager.revoke_user("alice")
    release.set()
    await asyncio.wait_for(sending, 1)
    assert len(device.output) == 1
    assert not setup.manager.is_connected("alice")
    assert device not in setup.manager._session_hashes


@pytest.mark.asyncio
async def test_revocation_between_handshake_lookup_and_connect_rejects_connection(setup, monkeypatch):
    original = auth.get_user_from_token

    async def lookup(token):
        user = await original(token)
        invalidate(setup.database, "revoked")
        return user

    monkeypatch.setattr(auth, "get_user_from_token", lookup)
    app = FastAPI()
    chat.register_websocket(app)
    endpoint = next(route.endpoint for route in app.routes if route.path == "/ws")
    device = Device()
    await asyncio.wait_for(endpoint(device), 1)
    assert device.output == []
    assert device.close.await_args.kwargs["code"] == 4001
    chat._initialize_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_auth_lookup_timeout_fails_closed(setup, monkeypatch):
    device, _ = await setup.start()
    monkeypatch.setattr("app.ws_manager.SESSION_CHECK_TIMEOUT_SECONDS", .01)

    async def hung(*_, **__):
        await asyncio.Event().wait()

    monkeypatch.setattr(auth, "get_user_from_session_hash", hung)
    await asyncio.wait_for(setup.manager.send("alice", {"type": "token", "text": "private"}), 1)
    assert len(device.output) == 1
    assert not setup.manager.has_connection("alice", device)


@pytest.mark.asyncio
async def test_revoke_all_preserves_reconnect_and_durable_work():
    manager = WSManager()
    first, second, replacement = Device(), Device(), Device()
    for device in (first, second):
        await manager.connect(device, "alice")
    closing, release = asyncio.Event(), asyncio.Event()

    async def close(**_):
        closing.set()
        await release.wait()

    first.close.side_effect = close
    durable = asyncio.create_task(asyncio.Event().wait())
    manager.track_task("alice", durable, survive_disconnect=True)
    revoked = asyncio.create_task(manager.revoke_user("alice"))
    await asyncio.wait_for(closing.wait(), 1)
    assert not manager.has_connection("alice", first)
    assert not manager.has_connection("alice", second)
    await manager.connect(replacement, "alice")
    replacement_task = asyncio.create_task(asyncio.Event().wait())
    manager.track_task("alice", replacement_task)
    release.set()
    await asyncio.wait_for(revoked, 1)
    await manager.disconnect("alice", first)  # The old endpoint's finally block.
    assert manager.has_connection("alice", replacement)
    assert not replacement_task.done()
    assert not durable.done()
    await manager.disconnect("alice", replacement)
    durable.cancel()
    await asyncio.gather(durable, return_exceptions=True)
