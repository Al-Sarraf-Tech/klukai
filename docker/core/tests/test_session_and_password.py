"""Tests for /api/session/info, /api/user/change-password, /api/admin/rate-limit/reset."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _mk_request(auth_token: str = "good") -> MagicMock:
    req = MagicMock()
    req.headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    return req


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
# /api/session/info
# ═══════════════════════════════════════════════════════════════════════════


class TestSessionInfo:
    @pytest.mark.asyncio
    async def test_no_bearer_returns_401_with_code(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/session/info", "GET")
        req = MagicMock()
        req.headers = {}  # no auth header
        resp = await handler(req)
        assert resp.status_code == 401
        import json
        body = json.loads(resp.body)
        assert body["code"] == "AUTH_REQUIRED"

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401_auth_invalid(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/session/info", "GET")
        with patch("app.auth.get_session_info", return_value=None):
            resp = await handler(_mk_request())
        assert resp.status_code == 401
        import json
        body = json.loads(resp.body)
        assert body["code"] == "AUTH_INVALID"

    @pytest.mark.asyncio
    async def test_returns_metadata(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/session/info", "GET")
        info = {
            "user_id": "alice",
            "created_at": "2026-04-19T12:00:00+00:00",
            "expires_at": "2026-04-26T12:00:00+00:00",
        }
        with patch("app.auth.get_session_info", return_value=info):
            data = await handler(_mk_request())
        assert data == info


# ═══════════════════════════════════════════════════════════════════════════
# /api/user/change-password
# ═══════════════════════════════════════════════════════════════════════════


class TestChangePassword:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from app.routes import ChangePasswordRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/change-password", "POST")
        req_body = ChangePasswordRequest(old_password="old", new_password="a"*16)
        with patch("app.routes._get_user_id", return_value=None):
            resp = await handler(req_body, _mk_request())
        assert resp.status_code == 401
        import json
        body = json.loads(resp.body)
        assert body["code"] == "AUTH_REQUIRED"

    @pytest.mark.asyncio
    async def test_wrong_old_password_returns_invalid(self):
        from app.routes import ChangePasswordRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/change-password", "POST")
        req_body = ChangePasswordRequest(old_password="wrong", new_password="a"*16)

        with patch("app.routes._get_user_id", return_value="alice"), \
             patch("app.auth.change_password", new=AsyncMock(return_value=False)):
            resp = await handler(req_body, _mk_request())

        assert resp.status_code == 400
        import json
        body = json.loads(resp.body)
        assert body["code"] == "AUTH_INVALID"

    @pytest.mark.asyncio
    async def test_success_returns_invalidation_flag(self):
        from app.routes import ChangePasswordRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/change-password", "POST")
        req_body = ChangePasswordRequest(old_password="correct", new_password="a"*16)

        with patch("app.routes._get_user_id", return_value="alice"), \
             patch("app.auth.change_password", new=AsyncMock(return_value=True)):
            data = await handler(req_body, _mk_request())

        assert data["ok"] is True
        assert data["sessions_invalidated"] is True

    def test_short_new_password_rejected_by_pydantic(self):
        """new_password min_length=8 enforced by Pydantic."""
        from app.routes import ChangePasswordRequest
        with pytest.raises(Exception):
            ChangePasswordRequest(old_password="x", new_password="short")


# ═══════════════════════════════════════════════════════════════════════════
# /api/admin/rate-limit/reset
# ═══════════════════════════════════════════════════════════════════════════


class TestAdminRateLimitReset:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/admin/rate-limit/reset", "POST")
        with patch("app.routes._get_user_id", return_value=None):
            resp = await handler(_mk_request(), user_id_target="alice", bucket="stats")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_non_admin_forbidden(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/admin/rate-limit/reset", "POST")
        with patch("app.routes._get_user_id", return_value="bob"):
            resp = await handler(_mk_request(), user_id_target="alice", bucket="stats")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_unknown_bucket_rejected(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/admin/rate-limit/reset", "POST")
        with patch("app.routes._get_user_id", return_value="jalsarraf"):
            resp = await handler(_mk_request(), user_id_target="alice",
                                 bucket="nonexistent-bucket")
        assert resp.status_code == 400
        import json
        body = json.loads(resp.body)
        assert body["code"] == "INPUT_INVALID"
        assert "known" in body

    @pytest.mark.asyncio
    async def test_admin_can_reset_known_bucket(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/admin/rate-limit/reset", "POST")
        with patch("app.routes._get_user_id", return_value="jalsarraf"), \
             patch("app.rate_limit.reset", new=AsyncMock()) as reset_mock:
            data = await handler(_mk_request(), user_id_target="alice", bucket="stats")

        assert data["ok"] is True
        assert data["user_id"] == "alice"
        assert data["bucket"] == "stats"
        reset_mock.assert_awaited_once_with("alice", "stats")


# ═══════════════════════════════════════════════════════════════════════════
# change_password backend behavior
# ═══════════════════════════════════════════════════════════════════════════


class TestChangePasswordBackend:
    @pytest.mark.asyncio
    async def test_rejects_short_password(self):
        from app.auth import change_password
        ok = await change_password("alice", "old", "short")
        assert ok is False

    @pytest.mark.asyncio
    async def test_rejects_empty_password(self):
        from app.auth import change_password
        ok = await change_password("alice", "old", "")
        assert ok is False

    @pytest.mark.asyncio
    async def test_rejects_wrong_old_password(self):
        from app import auth
        import bcrypt as _bc

        # Hash of "correct"
        real_hash = _bc.hashpw(b"correct", _bc.gensalt()).decode()

        conn = MagicMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=None)
        cur = AsyncMock()
        cur.fetchone = AsyncMock(return_value=(real_hash,))
        conn.execute = AsyncMock(return_value=cur)

        with patch("app.auth.get_conn_autocommit", return_value=conn):
            ok = await auth.change_password("alice", "wrong", "validnewpass123")

        assert ok is False

    @pytest.mark.asyncio
    async def test_success_invalidates_sessions(self):
        from app import auth
        import bcrypt as _bc

        real_hash = _bc.hashpw(b"correct", _bc.gensalt()).decode()

        conn = MagicMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=None)
        cur = AsyncMock()
        cur.fetchone = AsyncMock(return_value=(real_hash,))
        conn.execute = AsyncMock(return_value=cur)

        with patch("app.auth.get_conn_autocommit", return_value=conn):
            ok = await auth.change_password("alice", "correct", "x" * 16)

        assert ok is True
        # Among the execute calls should be an UPDATE + DELETE
        all_sqls = [str(call.args[0]) for call in conn.execute.await_args_list]
        assert any("UPDATE companion_users" in s for s in all_sqls)
        assert any("DELETE FROM companion_auth_sessions" in s for s in all_sqls)
