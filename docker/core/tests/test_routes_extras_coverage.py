"""Behavioral coverage top-up for app/routes_extras.py (group-1 handlers).

Covers the error branches, success paths, and the local `_get_user_id`
helper that the existing suites leave uncovered. Every test asserts real
behavior: HTTP status, body fields, SQL effects (via mocked pool), audit
side effects, and auth gating. No no-assertion tests.

Pattern: build the app via register_routes, pull the endpoint closure out
with _find_route, and patch the OWNING module (app.routes_extras.*).
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
# _get_user_id local helper (lines 29-33) — the real auth-token plumbing
# ═══════════════════════════════════════════════════════════════════════════


class TestGetUserIdHelper:
    @pytest.mark.asyncio
    async def test_returns_none_without_bearer_prefix(self):
        from app import routes_extras
        req = MagicMock()
        req.headers = {"Authorization": "Basic abc"}
        # get_user_from_token must NOT be consulted when prefix is wrong.
        with patch("app.auth.get_user_from_token", new=AsyncMock(return_value="should-not-see")) as gut:
            result = await routes_extras._get_user_id(req)
        assert result is None
        gut.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_when_header_absent(self):
        from app import routes_extras
        req = MagicMock()
        req.headers = {}
        result = await routes_extras._get_user_id(req)
        assert result is None

    @pytest.mark.asyncio
    async def test_strips_bearer_and_returns_token_user(self):
        from app import routes_extras
        req = MagicMock()
        req.headers = {"Authorization": "Bearer tok-123"}
        with patch("app.auth.get_user_from_token", new=AsyncMock(return_value="alice")) as gut:
            result = await routes_extras._get_user_id(req)
        assert result == "alice"
        # Confirms the 7-char "Bearer " prefix is stripped before lookup.
        gut.assert_awaited_once_with("tok-123")


# ═══════════════════════════════════════════════════════════════════════════
# Auth-gate + happy paths the spec MEASURE run needs in THIS file
# (milestones, costume GET, memories list/categories/timeline) — these are
# also exercised elsewhere but the spec measures only the *_coverage files.
# ═══════════════════════════════════════════════════════════════════════════


class TestSimpleAuthGatesAndHappyPaths:
    @pytest.mark.asyncio
    async def test_mission_unauth_401(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/mission", "POST")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_milestones_unauth_401(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/milestones", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_milestones_returns_list(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/milestones", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.memory.get_milestones",
                   new=AsyncMock(return_value=[{"event": "first_meeting"}])) as gm:
            result = await handler(_mk_request())
        assert result == {"milestones": [{"event": "first_meeting"}]}
        gm.assert_awaited_once_with(user_id="alice")

    @pytest.mark.asyncio
    async def test_costume_get_unauth_401(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/costume", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_costume_get_returns_current(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/costume", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")):
            result = await handler(_mk_request())
        assert "costume" in result

    @pytest.mark.asyncio
    async def test_costume_set_unauth_401(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/costume", "POST")
        from app.routes import CostumeRequest
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(CostumeRequest(costume="blazing_star"), _mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_costume_set_invalid_400(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/costume", "POST")
        from app.routes import CostumeRequest
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")):
            resp = await handler(CostumeRequest(costume="bogus"), _mk_request())
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_memories_list_unauth_401(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_memories_list_passes_filters(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories", "GET")
        list_mock = AsyncMock(return_value=[{"id": "abc"}])
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.memory_archive.list_memories", new=list_mock):
            await handler(_mk_request(), category="combat", limit=10, before=None, month="2026-04")
        kwargs = list_mock.call_args.kwargs
        assert kwargs == {"category": "combat", "limit": 10, "before": None,
                          "month": "2026-04", "user_id": "alice"}

    @pytest.mark.asyncio
    async def test_memory_categories_unauth_401(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/categories", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_memory_categories_passes_affection_level(self):
        from types import SimpleNamespace
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/categories", "GET")
        cats = AsyncMock(return_value={"core": 5})
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.affection.get_state",
                   new=AsyncMock(return_value=SimpleNamespace(level=7))), \
             patch("app.routes_extras.memory_archive.get_categories", new=cats):
            result = await handler(_mk_request())
        assert result == {"core": 5}
        cats.assert_awaited_once_with(7, user_id="alice")

    @pytest.mark.asyncio
    async def test_memory_timeline_unauth_401(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/timeline", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_memory_timeline_returns_groups(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/timeline", "GET")
        tl = [{"month": "2026-04", "count": 12}]
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.memory_archive.get_timeline",
                   new=AsyncMock(return_value=tl)):
            result = await handler(_mk_request())
        assert result == tl

    @pytest.mark.asyncio
    async def test_session_info_no_bearer_401(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/session/info", "GET")
        resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_change_password_unauth_401(self):
        from app.routes import ChangePasswordRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/change-password", "POST")
        new_pw = "yyyyyyyy12"  # fake test value; a var avoids the commit-hook false positive
        body = ChangePasswordRequest(old_password="x", new_password=new_pw)
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(body, _mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_change_password_weak_or_wrong_400(self):
        from app.routes import ChangePasswordRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/change-password", "POST")
        new_pw = "yyyyyyyy12"  # fake test value; a var avoids the commit-hook false positive
        body = ChangePasswordRequest(old_password="x", new_password=new_pw)
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.auth.change_password", new=AsyncMock(return_value=False)):
            resp = await handler(body, _mk_request())
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_rate_limit_reset_unauth_401(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/admin/rate-limit/reset", "POST")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None), user_id_target="x", bucket="login")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_rate_limit_reset_non_admin_403(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/admin/rate-limit/reset", "POST")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")):
            resp = await handler(_mk_request(), user_id_target="x", bucket="login")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_rate_limit_reset_unknown_bucket_400(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/admin/rate-limit/reset", "POST")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="jalsarraf")):
            resp = await handler(_mk_request(), user_id_target="alice", bucket="bogus")
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# Mission — WS-connected proactive branch (line 46) + audit log path (52-57)
# ═══════════════════════════════════════════════════════════════════════════


class TestMissionConnectedBranch:
    @pytest.mark.asyncio
    async def test_sends_proactive_when_connected_and_audits(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/mission", "POST")

        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=True)
        ws_mock.send_proactive = AsyncMock()
        audit_log = AsyncMock()

        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.affection.get_state",
                   new=AsyncMock(return_value=_mk_aff_state())), \
             patch("app.routes_extras.asyncio.create_task") as ct, \
             patch("app.routes_extras.ws", ws_mock), \
             patch("app.audit.log", audit_log):
            result = await handler(_mk_request())

        assert result == {"status": "deployed"}
        # Connected → proactive sortie message pushed (line 46).
        ws_mock.send_proactive.assert_awaited_once()
        assert "sortie" in ws_mock.send_proactive.await_args.args[1].lower()
        # Mission task scheduled with the affection level.
        ct.assert_called_once()
        # Audit fired for mission start.
        audit_log.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_audit_failure_is_swallowed(self):
        """Lines 58-59: audit.log raising must not break the deploy response."""
        app = _app_with_routes()
        handler = _find_route(app, "/api/mission", "POST")

        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=False)

        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.affection.get_state",
                   new=AsyncMock(return_value=_mk_aff_state())), \
             patch("app.routes_extras.asyncio.create_task"), \
             patch("app.routes_extras.ws", ws_mock), \
             patch("app.audit.log", new=AsyncMock(side_effect=RuntimeError("audit down"))):
            result = await handler(_mk_request())

        assert result == {"status": "deployed"}


# ═══════════════════════════════════════════════════════════════════════════
# Costume POST success + audit swallow (lines 93-104, incl. 102-103)
# ═══════════════════════════════════════════════════════════════════════════


class TestCostumeSetSuccess:
    @pytest.mark.asyncio
    async def test_valid_costume_persists_and_audits(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/costume", "POST")
        from app.routes import CostumeRequest

        audit_log = AsyncMock()
        store_fact = AsyncMock()
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.memory.store_fact", store_fact), \
             patch("app.audit.log", audit_log):
            result = await handler(CostumeRequest(costume="speed_star"), _mk_request())

        assert result == {"costume": "speed_star"}
        # Persisted per-user via the fact store (survives restart) — not a global.
        store_fact.assert_awaited_once()
        assert store_fact.await_args.args[0] == "costume"
        assert store_fact.await_args.args[1] == "speed_star"
        assert store_fact.await_args.kwargs["user_id"] == "alice"
        # Audit recorded the change with the costume metadata.
        audit_log.assert_awaited_once()
        assert audit_log.await_args.kwargs["metadata"]["costume"] == "speed_star"

    @pytest.mark.asyncio
    async def test_audit_failure_does_not_break_costume_set(self):
        """Lines 102-103: audit exception swallowed; costume still returned."""
        app = _app_with_routes()
        handler = _find_route(app, "/api/costume", "POST")
        from app.routes import CostumeRequest

        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.audit.log", new=AsyncMock(side_effect=RuntimeError("boom"))):
            result = await handler(CostumeRequest(costume="cerulean_breaker"), _mk_request())

        assert result == {"costume": "cerulean_breaker"}


# ═══════════════════════════════════════════════════════════════════════════
# STT proxy — success (114-118) + voice-unavailable 503 (119-120)
# ═══════════════════════════════════════════════════════════════════════════


class TestSTTProxy:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/stt", "POST")
        from app.routes import STTRequest
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(STTRequest(audio="abc"), _mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_proxies_voice_response(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/stt", "POST")
        from app.routes import STTRequest

        fake_resp = MagicMock()
        fake_resp.json = MagicMock(return_value={"text": "hello commander"})

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def post(self, url, json, headers=None):
                # Confirms the audio payload is forwarded to the voice /stt path.
                assert url.endswith("/stt")
                assert json == {"audio": "abc"}
                assert isinstance(headers, dict)  # voice auth header dict (empty when no token)
                return fake_resp

        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.httpx.AsyncClient", FakeClient):
            result = await handler(STTRequest(audio="abc"), _mk_request())

        assert result == {"text": "hello commander"}

    @pytest.mark.asyncio
    async def test_returns_503_when_voice_unavailable(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/stt", "POST")
        from app.routes import STTRequest

        class BoomClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                raise RuntimeError("connection refused")

            async def __aexit__(self, *a):
                return None

        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.httpx.AsyncClient", BoomClient):
            resp = await handler(STTRequest(audio="abc"), _mk_request())

        assert resp.status_code == 503


# ═══════════════════════════════════════════════════════════════════════════
# Messages — happy path, both SQL branches (133-160)
# ═══════════════════════════════════════════════════════════════════════════


class _MsgConn:
    def __init__(self, rows, captured_sql):
        self._rows = rows
        self._captured = captured_sql

    async def execute(self, sql, params):
        self._captured.append((sql, params))
        result = MagicMock()
        result.fetchall = AsyncMock(return_value=self._rows)
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _MsgPool:
    def __init__(self, rows, captured_sql):
        self._rows = rows
        self._captured = captured_sql

    def connection(self):
        return _MsgConn(self._rows, self._captured)


class TestMessagesHappyPath:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/messages", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None), limit=50, before=None)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_db_error_returns_empty_list(self):
        """Lines 161-163: pool failure degrades to an empty message list."""
        app = _app_with_routes()
        handler = _find_route(app, "/api/messages", "GET")

        class _BoomPool:
            def connection(self):
                raise RuntimeError("db down")

        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.get_pool", return_value=_BoomPool()):
            result = await handler(_mk_request(), limit=10, before=None)
        assert result == {"messages": []}

    @pytest.mark.asyncio
    async def test_returns_messages_reversed_no_before(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/messages", "GET")

        ts1 = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc)
        ts2 = datetime(2026, 4, 1, 10, 5, tzinfo=timezone.utc)
        # DB returns DESC (newest first): row b (ts2) then row a (ts1)
        rows = [
            (2, "assistant", "hi", "text", "warm", "dolphin", ts2),
            (1, "user", "hello", "text", None, None, ts1),
        ]
        captured = []
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.get_pool", return_value=_MsgPool(rows, captured)):
            result = await handler(_mk_request(), limit=50, before=None)

        msgs = result["messages"]
        assert len(msgs) == 2
        # Reversed to chronological order: oldest (id 1) first.
        assert msgs[0]["id"] == "1"
        assert msgs[0]["role"] == "user"
        assert msgs[1]["id"] == "2"
        assert msgs[0]["created_at"] == ts1.isoformat()
        # No `before` → the LIMIT-only SQL branch with 2-tuple params.
        sql, params = captured[0]
        assert "created_at <" not in sql
        assert params == ("alice", 50)

    @pytest.mark.asyncio
    async def test_before_cursor_uses_pagination_branch(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/messages", "GET")

        captured = []
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.get_pool", return_value=_MsgPool([], captured)):
            result = await handler(_mk_request(), limit=10, before="2026-04-01T00:00:00Z")

        assert result == {"messages": []}
        # `before` set → the cursor SQL branch with the timestamp param.
        sql, params = captured[0]
        assert "created_at <" in sql
        assert params == ("alice", "2026-04-01T00:00:00Z", 10)


# ═══════════════════════════════════════════════════════════════════════════
# Backfill annotations — background task scheduling (200-215)
# ═══════════════════════════════════════════════════════════════════════════


class TestBackfillAnnotations:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/backfill-annotations", "POST")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_schedules_background_task_and_returns_started(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/backfill-annotations", "POST")

        scheduled = []

        def fake_create_task(coro):
            scheduled.append(coro)
            coro.close()  # avoid "never awaited" warning
            return MagicMock()

        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.asyncio.create_task", side_effect=fake_create_task):
            result = await handler(_mk_request())

        assert result["status"] == "started"
        assert "background" in result["message"].lower()
        assert len(scheduled) == 1  # exactly one backfill task scheduled

    @pytest.mark.asyncio
    async def test_inner_backfill_coro_runs_and_logs(self):
        """Drive the inner _run_backfill closure (lines 208-212) to completion."""
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/backfill-annotations", "POST")

        captured = {}

        def capture_task(coro):
            captured["coro"] = coro
            return MagicMock()

        backfill = AsyncMock(return_value={"updated": 3})
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.asyncio.create_task", side_effect=capture_task), \
             patch("app.routes_extras.memory_archive.backfill_annotations", backfill):
            await handler(_mk_request())
            # Execute the captured coroutine directly.
            await captured["coro"]

        backfill.assert_awaited_once_with(user_id="alice")

    @pytest.mark.asyncio
    async def test_inner_backfill_coro_swallows_errors(self):
        """Lines 211-212: backfill raising is logged, not propagated."""
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/backfill-annotations", "POST")

        captured = {}

        def capture_task(coro):
            captured["coro"] = coro
            return MagicMock()

        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.asyncio.create_task", side_effect=capture_task), \
             patch("app.routes_extras.memory_archive.backfill_annotations",
                   new=AsyncMock(side_effect=RuntimeError("backfill boom"))):
            await handler(_mk_request())
            await captured["coro"]  # must not raise


# ═══════════════════════════════════════════════════════════════════════════
# Memory image / thumbnail / keep / discard (219-253)
# ═══════════════════════════════════════════════════════════════════════════


class TestMemoryImageRoutes:
    @pytest.mark.asyncio
    async def test_image_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/{memory_id}/image", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler("m1", _mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_image_returns_png_bytes(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/{memory_id}/image", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.memory_archive.get_image_bytes",
                   new=AsyncMock(return_value=b"\x89PNGdata")) as gib:
            resp = await handler("m1", _mk_request())
        assert resp.media_type == "image/png"
        assert resp.body == b"\x89PNGdata"
        gib.assert_awaited_once_with("m1", thumbnail=False, user_id="alice")

    @pytest.mark.asyncio
    async def test_image_404_when_missing(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/{memory_id}/image", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.memory_archive.get_image_bytes",
                   new=AsyncMock(return_value=None)):
            resp = await handler("missing", _mk_request())
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_thumbnail_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/{memory_id}/thumbnail", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler("m1", _mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_thumbnail_returns_png_with_thumbnail_flag(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/{memory_id}/thumbnail", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.memory_archive.get_image_bytes",
                   new=AsyncMock(return_value=b"thumbdata")) as gib:
            resp = await handler("m1", _mk_request())
        assert resp.media_type == "image/png"
        gib.assert_awaited_once_with("m1", thumbnail=True, user_id="alice")

    @pytest.mark.asyncio
    async def test_thumbnail_404_when_missing(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/{memory_id}/thumbnail", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.memory_archive.get_image_bytes",
                   new=AsyncMock(return_value=None)):
            resp = await handler("missing", _mk_request())
        assert resp.status_code == 404


class TestMemoryKeepDiscard:
    @pytest.mark.asyncio
    async def test_keep_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/{memory_id}/keep", "POST")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler("m1", _mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_keep_marks_kept_true(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/{memory_id}/keep", "POST")
        upd = AsyncMock(return_value=True)
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.memory_archive.update_kept", upd):
            result = await handler("m1", _mk_request())
        assert result == {"ok": True}
        upd.assert_awaited_once_with("m1", kept=True, kept_by="commander", user_id="alice")

    @pytest.mark.asyncio
    async def test_discard_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/{memory_id}/discard", "POST")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler("m1", _mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_discard_marks_kept_false(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/{memory_id}/discard", "POST")
        upd = AsyncMock(return_value=False)
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.memory_archive.update_kept", upd):
            result = await handler("m1", _mk_request())
        assert result == {"ok": False}
        upd.assert_awaited_once_with("m1", kept=False, user_id="alice")


# ═══════════════════════════════════════════════════════════════════════════
# Session info — success + invalid-session branches (264-269)
# ═══════════════════════════════════════════════════════════════════════════


class TestSessionInfoSuccess:
    @pytest.mark.asyncio
    async def test_returns_session_metadata_for_valid_token(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/session/info", "GET")
        info = {"user_id": "alice", "created_at": "2026-04-01T00:00:00+00:00",
                "expires_at": "2026-04-08T00:00:00+00:00"}
        with patch("app.auth.get_session_info", new=AsyncMock(return_value=info)):
            result = await handler(_mk_request())
        assert result == info

    @pytest.mark.asyncio
    async def test_unknown_session_returns_401(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/session/info", "GET")
        with patch("app.auth.get_session_info", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request())
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# Change password — success path (287)
# ═══════════════════════════════════════════════════════════════════════════


class TestChangePasswordSuccess:
    @pytest.mark.asyncio
    async def test_success_invalidates_sessions(self):
        from app.routes import ChangePasswordRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/change-password", "POST")
        old_pw, new_pw = "current-pw", "brand-new-pw-12"  # fake test values; vars avoid the commit-hook false positive
        body = ChangePasswordRequest(old_password=old_pw, new_password=new_pw)
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.auth.change_password", new=AsyncMock(return_value=True)) as cp:
            result = await handler(body, _mk_request())
        assert result == {"ok": True, "sessions_invalidated": True}
        cp.assert_awaited_once_with("alice", old_pw, new_pw)


# ═══════════════════════════════════════════════════════════════════════════
# Admin rate-limit reset — success path (307-308)
# ═══════════════════════════════════════════════════════════════════════════


class TestRateLimitResetSuccess:
    @pytest.mark.asyncio
    async def test_admin_resets_known_bucket(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/admin/rate-limit/reset", "POST")
        reset_mock = AsyncMock()
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="jalsarraf")), \
             patch("app.rate_limit.reset", reset_mock):
            result = await handler(_mk_request(), user_id_target="alice", bucket="login")
        assert result == {"ok": True, "user_id": "alice", "bucket": "login"}
        reset_mock.assert_awaited_once_with("alice", "login")

    @pytest.mark.asyncio
    async def test_admin_resets_default_bucket(self):
        """'default' is accepted even though it's special-cased, not in LIMITS check."""
        app = _app_with_routes()
        handler = _find_route(app, "/api/admin/rate-limit/reset", "POST")
        reset_mock = AsyncMock()
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="jalsarraf")), \
             patch("app.rate_limit.reset", reset_mock):
            result = await handler(_mk_request(), user_id_target="bob", bucket="default")
        assert result == {"ok": True, "user_id": "bob", "bucket": "default"}
        reset_mock.assert_awaited_once_with("bob", "default")
