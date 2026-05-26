"""Behavioral coverage top-up for app/routes_extras2.py (group-2 handlers).

Covers tribute full-success flow, tribute listing + crown jewel get/set,
audit verify-chain admin success, metrics db_pool fallback, memory-search
DB-error path, and the local `_get_user_id` helper. Every test asserts real
behavior. No no-assertion tests.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
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


def _mk_aff_state(score: int = 500, level: int = 5):
    st = MagicMock()
    st.score = score
    st.level = level
    st.level_name = "Trusted"
    return st


# ═══════════════════════════════════════════════════════════════════════════
# _get_user_id local helper (lines 22-26)
# ═══════════════════════════════════════════════════════════════════════════


class TestGetUserIdHelper:
    @pytest.mark.asyncio
    async def test_none_when_no_bearer(self):
        from app import routes_extras2
        req = MagicMock()
        req.headers = {"Authorization": "Token zzz"}
        with patch("app.auth.get_user_from_token", new=AsyncMock(return_value="x")) as gut:
            result = await routes_extras2._get_user_id(req)
        assert result is None
        gut.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_user_for_valid_bearer(self):
        from app import routes_extras2
        req = MagicMock()
        req.headers = {"Authorization": "Bearer abc999"}
        with patch("app.auth.get_user_from_token", new=AsyncMock(return_value="bob")) as gut:
            result = await routes_extras2._get_user_id(req)
        assert result == "bob"
        gut.assert_awaited_once_with("abc999")


# ═══════════════════════════════════════════════════════════════════════════
# Tribute POST — full success flow (lines 56-117)
# ═══════════════════════════════════════════════════════════════════════════


class TestTributeSuccess:
    @pytest.mark.asyncio
    async def test_full_success_bumps_affection_and_returns_body(self):
        from app.routes import TributeRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/tribute", "POST")

        aff = _mk_aff_state(score=300, level=3)
        save_state = AsyncMock()
        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=False)

        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.tributes.count_recent", new=AsyncMock(return_value=0)), \
             patch("app.tributes.save_tribute", new=AsyncMock(return_value="trib-uuid-1")) as save, \
             patch("app.routes_extras2.affection.get_state", new=AsyncMock(return_value=aff)), \
             patch("app.routes_extras2.affection._save_state", save_state), \
             patch("app.routes_extras2.ws", ws_mock), \
             patch("app.audit.log", new=AsyncMock()):
            result = await handler(
                TributeRequest(text="t" * 30, make_crown_jewel=True), _mk_request()
            )

        assert result["ok"] is True
        assert result["tribute_id"] == "trib-uuid-1"
        assert result["is_crown_jewel"] is True
        assert result["affection_bump"] == 20
        assert result["new_score"] == 320  # 300 + TRIBUTE_AFFECTION_BUMP
        assert result["mood_shift"] == "grateful"
        # Tribute persisted with captured affection score (300, pre-bump).
        assert save.await_args.kwargs["affection_at_time"] == 300
        assert save.await_args.kwargs["make_crown_jewel"] is True
        # Affection state saved with the bumped score.
        assert save_state.await_args.args[0].score == 320

    @pytest.mark.asyncio
    async def test_success_clamps_score_at_1000(self):
        from app.routes import TributeRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/tribute", "POST")

        aff = _mk_aff_state(score=990, level=9)
        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=False)

        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.tributes.count_recent", new=AsyncMock(return_value=0)), \
             patch("app.tributes.save_tribute", new=AsyncMock(return_value="t2")), \
             patch("app.routes_extras2.affection.get_state", new=AsyncMock(return_value=aff)), \
             patch("app.routes_extras2.affection._save_state", new=AsyncMock()), \
             patch("app.routes_extras2.ws", ws_mock), \
             patch("app.audit.log", new=AsyncMock()):
            result = await handler(
                TributeRequest(text="t" * 30, make_crown_jewel=False), _mk_request()
            )

        assert result["new_score"] == 1000  # min(1000, 990+20) clamped

    @pytest.mark.asyncio
    async def test_connected_pushes_ws_and_swallows_ws_error(self):
        """Lines 74-88: connected → WS proactive + affection push; WS error swallowed."""
        from app.routes import TributeRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/tribute", "POST")

        aff = _mk_aff_state(score=400, level=4)
        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=True)
        ws_mock.send_proactive = AsyncMock(side_effect=RuntimeError("ws down"))
        ws_mock.send_affection = AsyncMock()

        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.tributes.count_recent", new=AsyncMock(return_value=0)), \
             patch("app.tributes.save_tribute", new=AsyncMock(return_value="t3")), \
             patch("app.routes_extras2.affection.get_state", new=AsyncMock(return_value=aff)), \
             patch("app.routes_extras2.affection._save_state", new=AsyncMock()), \
             patch("app.routes_extras2.ws", ws_mock), \
             patch("app.audit.log", new=AsyncMock()):
            result = await handler(
                TributeRequest(text="t" * 30, make_crown_jewel=False), _mk_request()
            )

        # WS proactive attempted even though it raised; tribute still succeeds.
        ws_mock.send_proactive.assert_awaited_once()
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_connected_pushes_affection_when_ws_ok(self):
        """Line 83: when send_proactive succeeds, send_affection also fires."""
        from app.routes import TributeRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/tribute", "POST")

        aff = _mk_aff_state(score=400, level=4)
        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=True)
        ws_mock.send_proactive = AsyncMock()
        ws_mock.send_affection = AsyncMock()

        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.tributes.count_recent", new=AsyncMock(return_value=0)), \
             patch("app.tributes.save_tribute", new=AsyncMock(return_value="t4")), \
             patch("app.routes_extras2.affection.get_state", new=AsyncMock(return_value=aff)), \
             patch("app.routes_extras2.affection._save_state", new=AsyncMock()), \
             patch("app.routes_extras2.ws", ws_mock), \
             patch("app.audit.log", new=AsyncMock()):
            result = await handler(
                TributeRequest(text="t" * 30, make_crown_jewel=False), _mk_request()
            )

        assert result["ok"] is True
        ws_mock.send_proactive.assert_awaited_once()
        # send_affection carries the bumped score + the affection bump constant.
        ws_mock.send_affection.assert_awaited_once()
        assert ws_mock.send_affection.await_args.args[1] == 420  # new_score

    @pytest.mark.asyncio
    async def test_audit_failure_does_not_break_tribute(self):
        """Lines 107-108: audit.log raising is swallowed; tribute still succeeds."""
        from app.routes import TributeRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/tribute", "POST")

        aff = _mk_aff_state(score=200, level=2)
        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=False)

        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.tributes.count_recent", new=AsyncMock(return_value=0)), \
             patch("app.tributes.save_tribute", new=AsyncMock(return_value="t5")), \
             patch("app.routes_extras2.affection.get_state", new=AsyncMock(return_value=aff)), \
             patch("app.routes_extras2.affection._save_state", new=AsyncMock()), \
             patch("app.routes_extras2.ws", ws_mock), \
             patch("app.audit.log", new=AsyncMock(side_effect=RuntimeError("audit down"))):
            result = await handler(
                TributeRequest(text="t" * 30, make_crown_jewel=False), _mk_request()
            )

        assert result["ok"] is True
        assert result["tribute_id"] == "t5"

    @pytest.mark.asyncio
    async def test_save_failure_returns_500(self):
        """Lines 65-66: save_tribute returning None → INTERNAL_ERROR 500."""
        from app.routes import TributeRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/tribute", "POST")

        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.tributes.count_recent", new=AsyncMock(return_value=0)), \
             patch("app.routes_extras2.affection.get_state",
                   new=AsyncMock(return_value=_mk_aff_state())), \
             patch("app.tributes.save_tribute", new=AsyncMock(return_value=None)):
            resp = await handler(
                TributeRequest(text="t" * 30, make_crown_jewel=False), _mk_request()
            )
        assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from app.routes import TributeRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/tribute", "POST")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(
                TributeRequest(text="t" * 30, make_crown_jewel=False), _mk_request(token=None)
            )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_cooldown_returns_429(self):
        from app.routes import TributeRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/tribute", "POST")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.tributes.count_recent", new=AsyncMock(return_value=1)):
            resp = await handler(
                TributeRequest(text="t" * 30, make_crown_jewel=True), _mk_request()
            )
        assert resp.status_code == 429


# ═══════════════════════════════════════════════════════════════════════════
# Tribute listing + crown jewel get/set (lines 122-153)
# ═══════════════════════════════════════════════════════════════════════════


class TestTributeListing:
    @pytest.mark.asyncio
    async def test_list_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/tributes", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_list_clamps_limit_and_returns_count(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/tributes", "GET")
        list_mock = AsyncMock(return_value=[{"id": "a"}, {"id": "b"}])
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.tributes.list_tributes", list_mock):
            result = await handler(_mk_request(), limit=9999)
        assert result["count"] == 2
        assert result["tributes"] == [{"id": "a"}, {"id": "b"}]
        # limit clamped to 100 max.
        assert list_mock.await_args.kwargs["limit"] == 100

    @pytest.mark.asyncio
    async def test_list_clamps_limit_floor_to_1(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/tributes", "GET")
        list_mock = AsyncMock(return_value=[])
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.tributes.list_tributes", list_mock):
            await handler(_mk_request(), limit=0)
        assert list_mock.await_args.kwargs["limit"] == 1


class TestCrownJewel:
    @pytest.mark.asyncio
    async def test_get_crown_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/tribute/crown", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_get_crown_returns_jewel(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/tribute/crown", "GET")
        jewel = {"id": "j1", "text": "you are my home"}
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.tributes.get_crown_jewel", new=AsyncMock(return_value=jewel)):
            result = await handler(_mk_request())
        assert result == {"crown_jewel": jewel}

    @pytest.mark.asyncio
    async def test_set_crown_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/tributes/{tribute_id}/crown", "POST")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler("t1", _mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_set_crown_success(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/tributes/{tribute_id}/crown", "POST")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.tributes.set_crown_jewel", new=AsyncMock(return_value=True)) as scj:
            result = await handler("t1", _mk_request())
        assert result == {"ok": True, "crown_jewel_id": "t1"}
        scj.assert_awaited_once_with("alice", "t1")

    @pytest.mark.asyncio
    async def test_set_crown_not_found_404(self):
        """Lines 151-152: set_crown_jewel False → 404."""
        app = _app_with_routes()
        handler = _find_route(app, "/api/tributes/{tribute_id}/crown", "POST")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.tributes.set_crown_jewel", new=AsyncMock(return_value=False)):
            resp = await handler("missing", _mk_request())
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# Dreams happy path (auth covered elsewhere; ensure handler body runs here too)
# ═══════════════════════════════════════════════════════════════════════════


class TestDreams:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/dreams", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_count_total_and_items(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/dreams", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.dreams.list_dreams", new=AsyncMock(return_value=[{"id": "d1"}])), \
             patch("app.dreams.count_dreams", new=AsyncMock(return_value=7)):
            result = await handler(_mk_request(), limit=5)
        assert result == {"count": 1, "total": 7, "dreams": [{"id": "d1"}]}


# ═══════════════════════════════════════════════════════════════════════════
# Affection timeline — auth + clamp + happy + DB-error (175-212)
# ═══════════════════════════════════════════════════════════════════════════


class _TLConn:
    def __init__(self, rows, captured):
        self._rows = rows
        self._captured = captured

    async def execute(self, sql, params):
        self._captured.append(params)
        result = AsyncMock()
        result.fetchall = AsyncMock(return_value=self._rows)
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _TLPool:
    def __init__(self, rows, captured):
        self._rows = rows
        self._captured = captured

    def connection(self):
        return _TLConn(self._rows, self._captured)


class TestAffectionTimeline:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/affection-timeline", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_points_and_clamps_low(self):
        from datetime import date
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/affection-timeline", "GET")
        rows = [(date(2026, 4, 1), 120, 20, 3)]
        captured = []
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras2.get_pool", return_value=_TLPool(rows, captured)):
            result = await handler(_mk_request(), days=-99)
        assert result["days"] == 1  # clamped low
        assert result["count"] == 1
        assert result["points"][0]["end_score"] == 120
        assert result["points"][0]["date"] == "2026-04-01"
        assert captured[0][1] == 1

    @pytest.mark.asyncio
    async def test_db_error_returns_500(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/affection-timeline", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras2.get_pool", side_effect=RuntimeError("db down")):
            resp = await handler(_mk_request(), days=30)
        assert resp.status_code == 500


# ═══════════════════════════════════════════════════════════════════════════
# Audit verify-chain — admin success path (lines 229-248) + error (249-251)
# ═══════════════════════════════════════════════════════════════════════════


class _ChainConn:
    def __init__(self, rows, captured):
        self._rows = rows
        self._captured = captured

    async def execute(self, sql, params):
        self._captured.append(params)
        result = AsyncMock()
        result.fetchall = AsyncMock(return_value=self._rows)
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _ChainPool:
    def __init__(self, rows, captured):
        self._rows = rows
        self._captured = captured

    def connection(self):
        return _ChainConn(self._rows, self._captured)


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

    @pytest.mark.asyncio
    async def test_admin_verifies_chain_and_clamps_limit(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/audit/verify-chain", "GET")
        ts = datetime(2026, 4, 1, tzinfo=timezone.utc)
        rows = [
            (1, "login.success", "alice", "127.0.0.1", "rq1", {"k": "v"}, ts, "hash1"),
        ]
        captured = []
        verify = MagicMock(return_value={"valid": True, "break_at_id": None, "checked": 1})
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="jalsarraf")), \
             patch("app.routes_extras2.get_pool", return_value=_ChainPool(rows, captured)), \
             patch("app.audit_chain.verify_chain", verify):
            result = await handler(_mk_request(), limit=999999)

        assert result == {"valid": True, "break_at_id": None, "checked": 1}
        # limit clamped to 5000.
        assert captured[0][0] == 5000
        # verify_chain received the normalized row dicts.
        passed_rows = verify.call_args.args[0]
        assert passed_rows[0]["id"] == 1
        assert passed_rows[0]["event_type"] == "login.success"
        assert passed_rows[0]["chain_hash"] == "hash1"

    @pytest.mark.asyncio
    async def test_db_error_returns_500(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/audit/verify-chain", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="jalsarraf")), \
             patch("app.routes_extras2.get_pool", side_effect=RuntimeError("db down")):
            resp = await handler(_mk_request())
        assert resp.status_code == 500


# ═══════════════════════════════════════════════════════════════════════════
# Audit viewer (admin) — happy path
# ═══════════════════════════════════════════════════════════════════════════


class TestAuditViewer:
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
    async def test_admin_reads_events(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/audit", "GET")
        recent = AsyncMock(return_value=[{"event_type": "login.success"}])
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="jalsarraf")), \
             patch("app.audit.recent", recent):
            result = await handler(_mk_request(), limit=10, event_type="login.success",
                                   user_id_filter="alice")
        assert result["count"] == 1
        assert result["events"][0]["event_type"] == "login.success"
        recent.assert_awaited_once_with(limit=10, event_type="login.success", user_id="alice")


# ═══════════════════════════════════════════════════════════════════════════
# Metrics — admin happy path + db_pool unavailable fallback (298-299)
# ═══════════════════════════════════════════════════════════════════════════


class TestMetrics:
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
    async def test_admin_includes_db_pool_sizes(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/metrics", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="jalsarraf")), \
             patch("app.metrics.snapshot", return_value={"counters": {"x": 1}}), \
             patch("app.routes_extras2.get_pool",
                   return_value=MagicMock(min_size=2, max_size=8)):
            result = await handler(_mk_request())
        assert result["counters"] == {"x": 1}
        assert result["db_pool"] == {"min_size": 2, "max_size": 8}

    @pytest.mark.asyncio
    async def test_db_pool_unavailable_fallback(self):
        """Lines 298-299: pool access raising → db_pool status 'unavailable'."""
        app = _app_with_routes()
        handler = _find_route(app, "/api/metrics", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="jalsarraf")), \
             patch("app.metrics.snapshot", return_value={"counters": {}}), \
             patch("app.routes_extras2.get_pool", side_effect=RuntimeError("no pool")):
            result = await handler(_mk_request())
        assert result["db_pool"] == {"status": "unavailable"}


# ═══════════════════════════════════════════════════════════════════════════
# Memory search — happy path + short-query + DB-error 500 (337-339)
# ═══════════════════════════════════════════════════════════════════════════


class _SearchConn:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, sql, params):
        result = MagicMock()
        result.fetchall = AsyncMock(return_value=self._rows)
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _SearchPool:
    def __init__(self, rows):
        self._rows = rows

    def connection(self):
        return _SearchConn(self._rows)


class TestMemorySearch:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/search", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None), q="smile")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_short_query_400(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/search", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")):
            resp = await handler(_mk_request(), q="a")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_returns_results(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/search", "GET")
        ts = datetime(2026, 4, 1, tzinfo=timezone.utc)
        rows = [("m1", "img1.png", "she smiled", "slice_of_life", ["tag"], ts)]
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras2.get_pool", return_value=_SearchPool(rows)):
            result = await handler(_mk_request(), q="smile", limit=20)
        assert result["query"] == "smile"
        assert result["count"] == 1
        assert result["results"][0]["annotation"] == "she smiled"
        assert result["results"][0]["created_at"] == ts.isoformat()

    @pytest.mark.asyncio
    async def test_db_error_returns_500(self):
        """Lines 337-339: pool failure → search 500."""
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/search", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras2.get_pool", side_effect=RuntimeError("db down")):
            resp = await handler(_mk_request(), q="smile")
        assert resp.status_code == 500
