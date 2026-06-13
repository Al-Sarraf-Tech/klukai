"""Tests for remaining route handlers — admin + audit + metrics + user CRUD.

Same pattern as test_routes_auth_affection.py but covers handlers I
hadn't tested yet. Each test verifies route registration + auth gate +
admin gate where applicable.
"""

from __future__ import annotations

import sys
from pathlib import Path
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
# Audit + audit verify chain
# ═══════════════════════════════════════════════════════════════════════════


class TestAuditEndpoint:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/audit", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_non_admin_forbidden(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/audit", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")):
            resp = await handler(_mk_request())
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_can_read(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/audit", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="jalsarraf")), \
             patch("app.audit.recent", new=AsyncMock(return_value=[{"event_type": "login.success"}])):
            result = await handler(_mk_request())
        assert result["count"] == 1
        assert result["events"][0]["event_type"] == "login.success"


class TestAuditVerifyChain:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/audit/verify-chain", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_non_admin_forbidden(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/audit/verify-chain", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")):
            resp = await handler(_mk_request())
        assert resp.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# Metrics
# ═══════════════════════════════════════════════════════════════════════════


class TestMetricsEndpoint:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/metrics", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_non_admin_forbidden(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/metrics", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")):
            resp = await handler(_mk_request())
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_returns_snapshot(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/metrics", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="jalsarraf")), \
             patch("app.metrics.snapshot", return_value={"counters": {}}), \
             patch("app.routes_extras2.get_pool") as gp:
            gp.return_value = MagicMock(min_size=1, max_size=5)
            result = await handler(_mk_request())
        assert "counters" in result
        assert "db_pool" in result


# ═══════════════════════════════════════════════════════════════════════════
# Dreams
# ═══════════════════════════════════════════════════════════════════════════


class TestDreamsEndpoint:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/dreams", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_list_and_total(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/dreams", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.dreams.list_dreams", new=AsyncMock(return_value=[{"id": "d1"}])), \
             patch("app.dreams.count_dreams", new=AsyncMock(return_value=5)):
            result = await handler(_mk_request())
        assert result["count"] == 1
        assert result["total"] == 5


# ═══════════════════════════════════════════════════════════════════════════
# User stats + export + memories search
# ═══════════════════════════════════════════════════════════════════════════


class TestUserStatsEndpoint:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/stats", "GET")
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401


class TestUserExportEndpoint:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/export", "GET")
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401


class TestMemoriesSearchEndpoint:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/search", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None), q="test")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_rejects_short_query(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/search", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")):
            resp = await handler(_mk_request(), q="a")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_rejects_empty_query(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/search", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")):
            resp = await handler(_mk_request(), q="")
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# Affection timeline
# ═══════════════════════════════════════════════════════════════════════════


class TestAffectionTimelineEndpoint:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/affection-timeline", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_clamps_days_to_max_365(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/affection-timeline", "GET")

        captured_days = []

        class FakePool:
            def connection(self):
                pool = self

                class Conn:
                    async def execute(self_inner, sql, params):
                        captured_days.append(params[1])
                        result = AsyncMock()
                        result.fetchall = AsyncMock(return_value=[])
                        return result

                    async def __aenter__(self_inner):
                        return self_inner

                    async def __aexit__(self_inner, *a):
                        return None

                return Conn()

        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras2.get_pool", return_value=FakePool()):
            await handler(_mk_request(), days=99999)

        assert captured_days[0] == 365  # clamped


# ═══════════════════════════════════════════════════════════════════════════
# Session info + password change
# ═══════════════════════════════════════════════════════════════════════════


class TestSessionInfoEndpoint:
    @pytest.mark.asyncio
    async def test_no_bearer_returns_401(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/session/info", "GET")
        req = MagicMock()
        req.headers = {}
        req.client = MagicMock()
        req.client.host = "127.0.0.1"
        resp = await handler(req)
        assert resp.status_code == 401


class TestChangePasswordEndpoint:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from app.routes import ChangePasswordRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/change-password", "POST")
        old_p, new_p = "fixture-a", "fixture-b-with-12"
        body = ChangePasswordRequest(**{"old_password": old_p, "new_password": new_p})
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(body, _mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_old_password(self):
        from app.routes import ChangePasswordRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/change-password", "POST")
        old_p, new_p = "fixture-wrong", "fixture-new-with-12"
        body = ChangePasswordRequest(**{"old_password": old_p, "new_password": new_p})
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.auth.change_password", new=AsyncMock(return_value=False)):
            resp = await handler(body, _mk_request())
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# Admin rate limit reset
# ═══════════════════════════════════════════════════════════════════════════


class TestRateLimitResetEndpoint:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/admin/rate-limit/reset", "POST")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None), user_id_target="x", bucket="login")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_non_admin_forbidden(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/admin/rate-limit/reset", "POST")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")):
            resp = await handler(_mk_request(), user_id_target="x", bucket="login")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_unknown_bucket_rejected(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/admin/rate-limit/reset", "POST")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="jalsarraf")):
            resp = await handler(_mk_request(), user_id_target="alice", bucket="bogus")
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# Tribute endpoints (defense-in-depth, building on existing test_tributes)
# ═══════════════════════════════════════════════════════════════════════════


class TestTributeRoutes:
    def test_post_tribute_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/tribute", "POST") in paths

    def test_list_tributes_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/tributes", "GET") in paths

    def test_crown_jewel_get_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/tribute/crown", "GET") in paths

    @pytest.mark.asyncio
    async def test_post_tribute_requires_auth(self):
        from app.routes import TributeRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/tribute", "POST")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(
                TributeRequest(text="a" * 30, make_crown_jewel=False),
                _mk_request(token=None),
            )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_cooldown_blocks(self):
        from app.routes import TributeRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/tribute", "POST")
        # Simulate "1 recent tribute" → blocks
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.tributes.count_recent", new=AsyncMock(return_value=1)):
            resp = await handler(
                TributeRequest(text="a" * 30, make_crown_jewel=True),
                _mk_request(),
            )
        assert resp.status_code == 429  # cooldown active

    @pytest.mark.asyncio
    async def test_get_crown_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/tribute/crown", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401
