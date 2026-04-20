"""Tests for audit logging + /api/audit endpoint."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _mk_request() -> MagicMock:
    req = MagicMock()
    req.headers = {"Authorization": "Bearer good"}
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


class _FakeConn:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.executed_sqls: list[str] = []
        self.executed_params: list[tuple] = []

    async def execute(self, sql, params=None):
        self.executed_sqls.append(sql)
        self.executed_params.append(params or ())
        result = AsyncMock()
        result.fetchall = AsyncMock(return_value=self._rows)
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return self._conn


# ═══════════════════════════════════════════════════════════════════════════
# audit.log primitive
# ═══════════════════════════════════════════════════════════════════════════


class TestAuditLog:
    @pytest.mark.asyncio
    async def test_log_writes_insert(self):
        from app.audit import log
        conn = _FakeConn()
        pool = _FakePool(conn)
        with patch("app.audit.get_pool", return_value=pool):
            await log("test.event", user_id="alice", ip_address="1.2.3.4",
                      metadata={"key": "val"})
        assert any("INSERT INTO companion_audit_log" in s for s in conn.executed_sqls)

    @pytest.mark.asyncio
    async def test_log_serializes_metadata_as_json(self):
        from app.audit import log
        conn = _FakeConn()
        pool = _FakePool(conn)
        with patch("app.audit.get_pool", return_value=pool):
            await log("test", metadata={"a": 1, "b": "x"})
        params = conn.executed_params[0]
        assert "a" in params[-1] and "1" in params[-1]

    @pytest.mark.asyncio
    async def test_log_never_raises_on_db_failure(self):
        """Audit must fail silently — never block the primary path."""
        from app.audit import log

        def broken():
            raise RuntimeError("db down")

        with patch("app.audit.get_pool", side_effect=broken):
            await log("test.event")  # should not raise

    @pytest.mark.asyncio
    async def test_log_handles_null_user_id(self):
        from app.audit import log
        conn = _FakeConn()
        pool = _FakePool(conn)
        with patch("app.audit.get_pool", return_value=pool):
            await log("login.failure", user_id=None, ip_address="1.2.3.4")
        params = conn.executed_params[0]
        # user_id is second param (event_type, user_id, ip, request_id, metadata)
        assert params[1] is None


class TestEventTypes:
    def test_canonical_events_defined(self):
        from app import audit
        for attr in ("EVENT_LOGIN_SUCCESS", "EVENT_LOGIN_FAILURE",
                     "EVENT_EXPORT_REQUESTED", "EVENT_GIFT_GIVEN",
                     "EVENT_MISSION_STARTED"):
            assert hasattr(audit, attr), f"missing {attr}"

    def test_event_names_use_dotted_namespace(self):
        from app import audit
        for attr in dir(audit):
            if attr.startswith("EVENT_"):
                value = getattr(audit, attr)
                assert "." in value, f"{attr} should use dotted namespace, got {value!r}"


class TestRecent:
    @pytest.mark.asyncio
    async def test_recent_returns_empty_on_no_rows(self):
        from app.audit import recent
        conn = _FakeConn(rows=[])
        pool = _FakePool(conn)
        with patch("app.audit.get_pool", return_value=pool):
            events = await recent(limit=10)
        assert events == []

    @pytest.mark.asyncio
    async def test_recent_limit_clamped(self):
        from app.audit import recent
        conn = _FakeConn(rows=[])
        pool = _FakePool(conn)
        with patch("app.audit.get_pool", return_value=pool):
            await recent(limit=99999)
        params = conn.executed_params[0]
        # last param is limit
        assert params[-1] <= 1000

    @pytest.mark.asyncio
    async def test_recent_applies_event_type_filter(self):
        from app.audit import recent
        conn = _FakeConn(rows=[])
        pool = _FakePool(conn)
        with patch("app.audit.get_pool", return_value=pool):
            await recent(event_type="login.failure", limit=10)
        sql = conn.executed_sqls[0]
        assert "WHERE event_type = %s" in sql


# ═══════════════════════════════════════════════════════════════════════════
# /api/audit endpoint
# ═══════════════════════════════════════════════════════════════════════════


class TestAuditEndpoint:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/audit", "GET")
        with patch("app.routes._get_user_id", return_value=None):
            resp = await handler(_mk_request())
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_non_admin_forbidden(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/audit", "GET")
        with patch("app.routes._get_user_id", return_value="bob"):
            resp = await handler(_mk_request())
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_can_read(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/audit", "GET")

        sample = [{"id": 1, "event_type": "login.success", "user_id": "alice"}]
        with patch("app.routes._get_user_id", return_value="jalsarraf"), \
             patch("app.audit.recent", new=AsyncMock(return_value=sample)):
            data = await handler(_mk_request())
        assert data["count"] == 1
        assert data["events"] == sample
