"""Tests for the auth/affection routes in app.routes.

Pattern adapted from test_user_api.py: build a fresh FastAPI app,
register routes, locate the handler by path/method, call it directly
with mocked Request and patched dependencies. This bypasses TestClient
setup overhead and gives us per-route unit-test isolation.

Note: these tests target CI's Python 3.13 env. Local Python 3.14
fails the `register_routes` import due to fastapi/starlette Router
API change (deprecated `on_startup` kwarg). This is consistent with
the existing test_user_api.py / test_session_and_password.py suite —
they pass in CI.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _mk_request(token: str | None = "good") -> MagicMock:
    req = MagicMock()
    req.headers = {"Authorization": f"Bearer {token}"} if token else {}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    req.state = MagicMock()
    req.state.request_id = "test-req-id"
    return req


def _mk_affection(score: int = 500, level: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        score=score,
        level=level,
        level_name="Trust Established",
        consecutive_days=7,
        total_interactions=100,
        first_interaction=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _app_with_routes() -> FastAPI:
    from app.routes import register_routes
    app = FastAPI()
    register_routes(app)
    return app


def _find_route(app: FastAPI, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


# ═══════════════════════════════════════════════════════════════════════════
# Auth routes
# ═══════════════════════════════════════════════════════════════════════════


class TestLoginRoute:
    def test_route_registered(self):
        app = _app_with_routes()
        paths = {(route.path, m) for route in app.routes for m in getattr(route, "methods", set())}
        assert ("/api/auth/login", "POST") in paths

    @pytest.mark.asyncio
    async def test_returns_token_on_success(self):
        from app.routes import LoginRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/auth/login", "POST")

        with patch("app.auth.check_ip_banned", new=AsyncMock(return_value=False)), \
             patch("app.auth.authenticate", new=AsyncMock(return_value="token-xyz")):
            result = await handler(
                LoginRequest(username="alice", password="hunter2"),
                _mk_request(),
            )
        assert result == {"token": "token-xyz", "user_id": "alice"}

    @pytest.mark.asyncio
    async def test_returns_401_on_bad_creds(self):
        from app.routes import LoginRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/auth/login", "POST")

        with patch("app.auth.check_ip_banned", new=AsyncMock(return_value=False)), \
             patch("app.auth.authenticate", new=AsyncMock(return_value=None)):
            resp = await handler(
                LoginRequest(username="alice", password="wrong"),
                _mk_request(),
            )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_403_when_ip_banned(self):
        from app.routes import LoginRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/auth/login", "POST")

        with patch("app.auth.check_ip_banned", new=AsyncMock(return_value=True)):
            resp = await handler(
                LoginRequest(username="alice", password="any"),
                _mk_request(),
            )
        assert resp.status_code == 403


class TestVerifyTokenRoute:
    def test_route_registered(self):
        app = _app_with_routes()
        paths = {(route.path, m) for route in app.routes for m in getattr(route, "methods", set())}
        assert ("/api/auth/verify", "GET") in paths

    @pytest.mark.asyncio
    async def test_returns_user_id_on_valid_token(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/auth/verify", "GET")

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")):
            result = await handler(_mk_request())
        assert result == {"user_id": "alice"}

    @pytest.mark.asyncio
    async def test_returns_401_on_invalid_token(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/auth/verify", "GET")

        with patch("app.routes._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# Affection route
# ═══════════════════════════════════════════════════════════════════════════


class TestAffectionRoute:
    def test_route_registered(self):
        app = _app_with_routes()
        paths = {(route.path, m) for route in app.routes for m in getattr(route, "methods", set())}
        assert ("/api/affection", "GET") in paths

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/affection", "GET")

        with patch("app.routes._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_affection_state(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/affection", "GET")

        from app.models import SessionState
        # Use a SessionState-shaped Pydantic model for model_dump()
        state = MagicMock(spec=SessionState)
        state.model_dump = MagicMock(return_value={"score": 500, "level": 5})

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes.affection.get_state", new=AsyncMock(return_value=state)):
            result = await handler(_mk_request())

        assert result == {"score": 500, "level": 5}


# ═══════════════════════════════════════════════════════════════════════════
# Push subscription + vapid
# ═══════════════════════════════════════════════════════════════════════════


class TestVapidRoute:
    def test_route_registered(self):
        app = _app_with_routes()
        paths = {(route.path, m) for route in app.routes for m in getattr(route, "methods", set())}
        assert ("/api/vapid-key", "GET") in paths

    @pytest.mark.asyncio
    async def test_returns_public_key(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/vapid-key", "GET")

        with patch("app.routes.get_vapid_public_key", return_value="public-key-data"):
            result = await handler()
        assert result == {"key": "public-key-data"}


class TestPushSubscribeRoute:
    def test_route_registered(self):
        app = _app_with_routes()
        paths = {(route.path, m) for route in app.routes for m in getattr(route, "methods", set())}
        assert ("/api/push/subscribe", "POST") in paths

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from app.routes import PushSubscription
        app = _app_with_routes()
        handler = _find_route(app, "/api/push/subscribe", "POST")

        with patch("app.routes._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(
                PushSubscription(endpoint="https://x.example/push", keys={"p256dh": "x", "auth": "y"}),
                _mk_request(token=None),
            )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_persists_subscription(self):
        from app.routes import PushSubscription
        app = _app_with_routes()
        handler = _find_route(app, "/api/push/subscribe", "POST")

        sub = PushSubscription(endpoint="https://x.example/push", keys={"p256dh": "x", "auth": "y"})
        add_mock = AsyncMock()

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes.add_subscription", new=add_mock):
            result = await handler(sub, _mk_request())

        assert result == {"ok": True}
        add_mock.assert_called_once()
        # Subscription should be persisted under the authenticated user
        called_user = add_mock.call_args.args[0]
        assert called_user == "alice"
