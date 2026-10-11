"""Behavioral coverage top-up for app/routes_extras3.py (group-3 handlers).

Covers user-stats DB-error path, export audit + memories branch, billing
tiers/subscription/usage, the Stripe webhook no-op, account deactivation
(success + failure), the self-destructing service worker, the root login
page, and the local `_get_user_id` helper. Every test asserts real
behavior. No no-assertion tests.
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


def _mk_aff(score: int = 500, level: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        score=score, level=level, level_name="Trusted",
        consecutive_days=7, total_interactions=100,
        first_interaction=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


# ═══════════════════════════════════════════════════════════════════════════
# _get_user_id local helper (lines 22-26)
# ═══════════════════════════════════════════════════════════════════════════


class TestGetUserIdHelper:
    @pytest.mark.asyncio
    async def test_none_without_bearer(self):
        from app import routes_extras3
        req = MagicMock()
        req.headers = {}
        result = await routes_extras3._get_user_id(req)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_user_for_valid_bearer(self):
        from app import routes_extras3
        req = MagicMock()
        req.headers = {"Authorization": "Bearer xyz-7"}
        with patch("app.auth.get_user_from_token", new=AsyncMock(return_value="carol")) as gut:
            result = await routes_extras3._get_user_id(req)
        assert result == "carol"
        gut.assert_awaited_once_with("xyz-7")


# ═══════════════════════════════════════════════════════════════════════════
# User stats — auth + DB-error 500 (101-103)
# ═══════════════════════════════════════════════════════════════════════════


class TestUserStats:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/stats", "GET")
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_db_error_returns_500(self):
        """Lines 101-103: pool failure → stats 500."""
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/stats", "GET")
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras3.get_pool", side_effect=RuntimeError("db down")):
            resp = await handler(_mk_request())
        assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_happy_path_aggregates(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/stats", "GET")

        results = [
            (42, 5, datetime(2026, 1, 1, tzinfo=timezone.utc),
             datetime(2026, 4, 20, tzinfo=timezone.utc)),
            (20,), (10, 8, 5), (3,), (4,),
        ]

        class FakeConn:
            def __init__(self):
                self._i = 0

            async def execute(self, sql, params):
                r = MagicMock()
                r.fetchone = AsyncMock(return_value=results[self._i])
                self._i += 1
                return r

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

        class FakePool:
            def connection(self):
                return FakeConn()

        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras3.get_pool", return_value=FakePool()), \
             patch("app.routes_extras3.affection.get_state",
                   new=AsyncMock(return_value=_mk_aff())):
            data = await handler(_mk_request())

        assert data["total_messages"] == 42
        assert data["klukai_messages"] == 22  # 42 - 20
        assert data["memories"]["kept"] == 8
        assert data["gifts_given"] == 3
        assert data["milestones_reached"] == 4


# ═══════════════════════════════════════════════════════════════════════════
# Export — auth + full bundle w/ audit (163-181, 192-209) + DB-error (210-212)
# ═══════════════════════════════════════════════════════════════════════════


class TestUserExport:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/export", "GET")
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_full_bundle_with_memories_and_audit(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/export", "GET")

        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # Order: messages, firsts, gifts, wardrobe history, memories
        batches = [
            [("user", "hi", "text", "warm", None, ts)],
            [("first_message", ts, {"x": 1})],
            [("flower", "pretty", "delighted", ts)],
            [(ts.date(), "winter_patrol", "It's 2°C.", None),
             (ts.date(), "blazing_star", "A duty day.", "immaculate_service")],
            [("annot", "slice_of_life", ["t"], "prompt", ts)],
        ]

        class FakeConn:
            def __init__(self):
                self._i = 0

            async def execute(self, sql, params):
                b = batches[self._i]
                self._i += 1
                r = MagicMock()
                r.fetchall = AsyncMock(return_value=b)
                return r

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

        class FakePool:
            def connection(self):
                return FakeConn()

        audit_log = AsyncMock()
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras3.get_pool", return_value=FakePool()), \
             patch("app.routes_extras3.affection.get_state",
                   new=AsyncMock(return_value=_mk_aff())), \
             patch("app.audit.log", audit_log):
            data = await handler(_mk_request(), include_memories=True, include_messages=True)

        assert data["user_id"] == "alice"
        assert len(data["messages"]) == 1
        assert len(data["milestones"]) == 1
        assert len(data["gifts"]) == 1
        assert data["wardrobe_history"][0] == {
            "day": "2026-01-01", "outfit": "winter_patrol", "reason": "It's 2°C.", "requested": False,
        }
        assert data["wardrobe_history"][1]["outfit"] == "immaculate_service"
        assert data["wardrobe_history"][1]["requested"] is True
        # include_memories=True → memories_kept branch (163-179) executed.
        assert len(data["memories_kept"]) == 1
        assert data["memories_kept"][0]["annotation"] == "annot"
        assert data["affection_snapshot"]["level"] == 5
        # Export audited with counts.
        audit_log.assert_awaited_once()
        assert audit_log.await_args.kwargs["metadata"]["message_count"] == 1
        assert audit_log.await_args.kwargs["metadata"]["memory_count"] == 1

    @pytest.mark.asyncio
    async def test_audit_failure_swallowed(self):
        """Lines 207-208: audit.log raising must not break the export."""
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/export", "GET")

        class FakeConn:
            async def execute(self, sql, params):
                r = MagicMock()
                r.fetchall = AsyncMock(return_value=[])
                return r

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

        class FakePool:
            def connection(self):
                return FakeConn()

        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras3.get_pool", return_value=FakePool()), \
             patch("app.routes_extras3.affection.get_state",
                   new=AsyncMock(return_value=_mk_aff())), \
             patch("app.audit.log", new=AsyncMock(side_effect=RuntimeError("audit down"))):
            data = await handler(_mk_request())
        assert data["user_id"] == "alice"

    @pytest.mark.asyncio
    async def test_db_error_returns_500(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/user/export", "GET")
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras3.get_pool", side_effect=RuntimeError("db down")):
            resp = await handler(_mk_request())
        assert resp.status_code == 500


# ═══════════════════════════════════════════════════════════════════════════
# Billing — tiers / subscription / usage (226-254)
# ═══════════════════════════════════════════════════════════════════════════


class TestBilling:
    @pytest.mark.asyncio
    async def test_tiers_public_personal_mode(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/billing/tiers", "GET")
        result = await handler()
        assert result["mode"] == "personal"
        # Real feature matrix surfaced.
        assert "free" in result["features"]
        assert "elite" in result["features"]

    @pytest.mark.asyncio
    async def test_subscription_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/billing/subscription", "GET")
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_subscription_returns_tier_shape(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/billing/subscription", "GET")
        sub = SimpleNamespace(
            tier="elite", status="active", is_active=True,
            period_start=None, period_end=None,
            features={"voice_enabled": True},
        )
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.billing.get_subscription", new=AsyncMock(return_value=sub)):
            result = await handler(_mk_request())
        assert result["tier"] == "elite"
        assert result["status"] == "active"
        assert result["is_active"] is True
        assert result["period_start"] is None
        assert result["features"] == {"voice_enabled": True}

    @pytest.mark.asyncio
    async def test_subscription_serializes_periods(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/billing/subscription", "GET")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 2, 1, tzinfo=timezone.utc)
        sub = SimpleNamespace(
            tier="pro", status="active", is_active=True,
            period_start=start, period_end=end, features={},
        )
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.billing.get_subscription", new=AsyncMock(return_value=sub)):
            result = await handler(_mk_request())
        assert result["period_start"] == start.isoformat()
        assert result["period_end"] == end.isoformat()

    @pytest.mark.asyncio
    async def test_usage_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/billing/usage", "GET")
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_usage_returns_summary(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/billing/usage", "GET")
        summary = {"tier": "elite", "counters": {}}
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.billing.get_usage_summary", new=AsyncMock(return_value=summary)) as gus:
            result = await handler(_mk_request())
        assert result == summary
        gus.assert_awaited_once_with("alice")


# ═══════════════════════════════════════════════════════════════════════════
# Stripe webhook — bad signature + bad JSON + dispatch (256-272)
# ═══════════════════════════════════════════════════════════════════════════


class TestStripeWebhook:
    @pytest.mark.asyncio
    async def test_invalid_signature_400(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/billing/webhook", "POST")
        req = MagicMock()
        req.body = AsyncMock(return_value=b"{}")
        req.headers = {"Stripe-Signature": "bad"}
        with patch("app.billing.verify_stripe_signature", return_value=False):
            resp = await handler(req)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_valid_sig_bad_json_400(self):
        """Lines 270-271: JSON parse failure after a valid signature → 400."""
        app = _app_with_routes()
        handler = _find_route(app, "/api/billing/webhook", "POST")
        req = MagicMock()
        req.body = AsyncMock(return_value=b"not-json{")
        req.headers = {"Stripe-Signature": "ok"}
        with patch("app.billing.verify_stripe_signature", return_value=True):
            resp = await handler(req)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_valid_event_dispatched(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/billing/webhook", "POST")
        req = MagicMock()
        req.body = AsyncMock(return_value=b'{"type": "ping"}')
        req.headers = {"Stripe-Signature": "ok"}
        handle = AsyncMock(return_value={"ok": True})
        with patch("app.billing.verify_stripe_signature", return_value=True), \
             patch("app.billing.handle_stripe_event", handle):
            result = await handler(req)
        assert result == {"ok": True}
        handle.assert_awaited_once_with({"type": "ping"})


# ═══════════════════════════════════════════════════════════════════════════
# Account deactivate — auth + success SQL + audit + DB-error (288-324)
# ═══════════════════════════════════════════════════════════════════════════


class TestAccountDeactivate:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from app.routes import AccountDeactivateRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/account/deactivate", "POST")
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(AccountDeactivateRequest(confirm="DEACTIVATE"),
                                 _mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_success_runs_sql_and_audits(self):
        from app.routes import AccountDeactivateRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/account/deactivate", "POST")

        executed = []

        class FakeConn:
            async def execute(self, sql, params=None):
                executed.append(sql)
                return MagicMock()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_autocommit():
            yield FakeConn()

        audit_log = AsyncMock()
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.db.get_conn_autocommit", fake_autocommit), \
             patch("app.audit.log", audit_log):
            result = await handler(AccountDeactivateRequest(confirm="DEACTIVATE"), _mk_request())

        assert result["deactivated"] is True
        assert result["user_id"] == "alice"
        # Two statements: UPDATE deactivated_at, DELETE sessions. The column is
        # created by migration 130, NOT a per-request ALTER (which took an
        # ACCESS EXCLUSIVE lock every call). Sessions live in the real table.
        joined = " ".join(executed)
        assert "ADD COLUMN" not in joined  # DDL must stay out of the request path
        assert "UPDATE companion_users SET deactivated_at = NOW()" in joined
        assert "DELETE FROM companion_auth_sessions" in joined
        # Audit recorded with the SACRED-preserved flag.
        audit_log.assert_awaited_once()
        assert audit_log.await_args.kwargs["metadata"]["sacred_chat_preserved"] is True

    @pytest.mark.asyncio
    async def test_audit_failure_swallowed_still_deactivates(self):
        """Lines 315-316: audit.log raising after SQL succeeds → still 'deactivated'."""
        from app.routes import AccountDeactivateRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/account/deactivate", "POST")

        class FakeConn:
            async def execute(self, sql, params=None):
                return MagicMock()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_autocommit():
            yield FakeConn()

        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.db.get_conn_autocommit", fake_autocommit), \
             patch("app.audit.log", new=AsyncMock(side_effect=RuntimeError("audit down"))):
            result = await handler(AccountDeactivateRequest(confirm="DEACTIVATE"), _mk_request())
        assert result["deactivated"] is True

    @pytest.mark.asyncio
    async def test_db_error_returns_500(self):
        from app.routes import AccountDeactivateRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/account/deactivate", "POST")
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.db.get_conn_autocommit", side_effect=RuntimeError("db down")):
            resp = await handler(AccountDeactivateRequest(confirm="DEACTIVATE"), _mk_request())
        assert resp.status_code == 500


# ═══════════════════════════════════════════════════════════════════════════
# Service worker + root (328-363)
# ═══════════════════════════════════════════════════════════════════════════


class TestServiceWorkerAndRoot:
    @pytest.mark.asyncio
    async def test_service_worker_self_destructs(self):
        app = _app_with_routes()
        handler = _find_route(app, "/flutter_service_worker.js", "GET")
        resp = await handler()
        assert resp.media_type == "application/javascript"
        body = resp.body.decode()
        # The SW must unregister itself and clear all caches.
        assert "unregister()" in body
        assert "caches.delete" in body
        assert "skipWaiting" in body
        assert resp.headers["Cache-Control"] == "no-cache, no-store, must-revalidate"

    @pytest.mark.asyncio
    async def test_root_serves_login_when_present(self):
        app = _app_with_routes()
        handler = _find_route(app, "/", "GET")

        class FakePath:
            def __init__(self, p):
                self._p = p

            def exists(self):
                return True

        sentinel = object()
        with patch("pathlib.Path", FakePath), \
             patch("fastapi.responses.FileResponse", return_value=sentinel) as fr:
            result = await handler()
        assert result is sentinel
        fr.assert_called_once()
        assert fr.call_args.kwargs["media_type"] == "text/html"
        # Revalidate every load: without a policy Safari keeps a stale
        # login.html for days after a redeploy (same rule as /app entry points).
        assert fr.call_args.kwargs["headers"] == {"Cache-Control": "no-cache"}

    @pytest.mark.asyncio
    async def test_root_fallback_when_login_missing(self):
        """Lines 363: no login.html on disk → JSON status fallback."""
        app = _app_with_routes()
        handler = _find_route(app, "/", "GET")

        class FakePath:
            def __init__(self, p):
                self._p = p

            def exists(self):
                return False

        with patch("pathlib.Path", FakePath):
            result = await handler()
        assert result["status"] == "companion-core running"
        assert "login page not deployed" in result["auth"]


class TestPromisesEndpoints:
    @pytest.mark.asyncio
    async def test_list_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/promises", "GET")
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_list_returns_open_promises(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/promises", "GET")
        sample = [{"id": "p1", "promise_text": "I'll rest", "commitment": {}}]
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.promises.open_promises", new=AsyncMock(return_value=sample)):
            result = await handler(_mk_request())
        assert result == {"promises": sample}

    @pytest.mark.asyncio
    async def test_resolve_rejects_bad_sentiment(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/promises/{promise_id}/resolve", "POST")
        req = _mk_request()
        req.json = AsyncMock(return_value={"sentiment": "nonsense"})
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")):
            resp = await handler("p1", req)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_resolve_confirms(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/promises/{promise_id}/resolve", "POST")
        req = _mk_request()
        req.json = AsyncMock(return_value={"sentiment": "confirmed", "response_text": "did it"})
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.promises.resolve_promise", new=AsyncMock(return_value=True)) as rp:
            result = await handler("p1", req)
        assert result == {"resolved": True}
        # Owner-scoped: the authenticated user_id is threaded into the resolve.
        rp.assert_awaited_once_with("p1", "confirmed", "did it", user_id="alice")


# ═══════════════════════════════════════════════════════════════════════════
# Her POV — start (rate-limited: LLM + GPU render) and status poll (not)
# ═══════════════════════════════════════════════════════════════════════════


class TestHerPov:
    @pytest.mark.asyncio
    async def test_start_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/her-pov", "POST")
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value=None)), \
             patch("app.rate_limit.check_and_consume", new=AsyncMock()) as cac:
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401
        cac.assert_not_awaited()  # nothing charged to an anonymous caller

    @pytest.mark.asyncio
    async def test_start_charges_image_gen_then_starts_the_job(self):
        import json
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/her-pov", "POST")
        started = {"job_id": "j1", "status": "queued", "reused": False}
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.rate_limit.check_and_consume",
                   new=AsyncMock(return_value=(19, 3600))) as cac, \
             patch("app.memory_her_pov.start_her_pov",
                   new=AsyncMock(return_value=started)) as start:
            resp = await handler(_mk_request())
        assert resp.status_code == 202
        assert json.loads(resp.body) == started
        # The expensive path shares the image_gen bucket (20 / hour).
        cac.assert_awaited_once_with("alice", "image_gen")
        start.assert_awaited_once_with("alice")

    @pytest.mark.asyncio
    async def test_start_over_limit_is_a_standard_429_and_renders_nothing(self):
        import json
        from app.rate_limit import RateLimitExceeded
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/her-pov", "POST")
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.rate_limit.check_and_consume",
                   new=AsyncMock(side_effect=RateLimitExceeded("image_gen", 1200))), \
             patch("app.memory_her_pov.start_her_pov", new=AsyncMock()) as start:
            resp = await handler(_mk_request())
        assert resp.status_code == 429
        assert json.loads(resp.body) == {
            "error": "Too many requests", "code": "RATE_LIMITED", "retry_after": 1200,
        }
        assert resp.headers["Retry-After"] == "1200"
        start.assert_not_awaited()  # no LLM call, no GPU job

    @pytest.mark.asyncio
    async def test_status_poll_is_not_charged(self):
        """The GET poll shares the path prefix with the POST; it must not be
        throttled — neither by the (method-blind) middleware table nor in the
        handler — or the client loses sight of a job it already paid for."""
        from app.main import _bucket_for_path
        assert _bucket_for_path("/api/memories/her-pov") is None
        assert _bucket_for_path("/api/memories/her-pov/j1") is None

        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/her-pov/{job_id}", "GET")
        job = {"id": "j1", "user_id": "alice", "status": "queued", "phase": "queued",
               "image_b64": "x" * 100}
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.rate_limit.check_and_consume", new=AsyncMock()) as cac, \
             patch("app.memory_her_pov.get_job", new=AsyncMock(return_value=job)):
            result = await handler("j1", _mk_request())
        cac.assert_not_awaited()
        assert result == {"id": "j1", "status": "queued", "phase": "queued"}  # bulk stripped

    @pytest.mark.asyncio
    async def test_status_poll_hides_another_users_job(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/her-pov/{job_id}", "GET")
        with patch("app.routes_extras3._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.memory_her_pov.get_job",
                   new=AsyncMock(return_value={"id": "j1", "user_id": "bob"})):
            resp = await handler("j1", _mk_request())
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# /internal/* hooks — shared-secret gate (_internal_token_ok)
# ═══════════════════════════════════════════════════════════════════════════


def _internal_req(token: str | None) -> MagicMock:
    req = MagicMock()
    req.headers = {"X-Internal-Token": token} if token is not None else {}
    req.json = AsyncMock(return_value={"task_id": "t1", "job_id": "j1"})
    return req


class TestInternalTokenGate:
    def test_unset_secret_fails_closed(self, monkeypatch):
        from app.routes_extras3 import _internal_token_ok
        monkeypatch.delenv("CORE_INTERNAL_TOKEN", raising=False)
        assert _internal_token_ok(_internal_req("anything")) is False
        assert _internal_token_ok(_internal_req("")) is False

    def test_matching_token_passes_anything_else_does_not(self, monkeypatch):
        from app.routes_extras3 import _internal_token_ok
        monkeypatch.setenv("CORE_INTERNAL_TOKEN", "s3cret")
        assert _internal_token_ok(_internal_req("s3cret")) is True
        assert _internal_token_ok(_internal_req("s3cret ")) is False
        assert _internal_token_ok(_internal_req("S3CRET")) is False
        assert _internal_token_ok(_internal_req(None)) is False

    def test_non_ascii_header_is_refused_not_an_error(self, monkeypatch):
        """Starlette hands a header byte ≥0x80 through as latin-1 text;
        compare_digest on str would raise TypeError and the hook would 500
        (and so leak that something is there). It must simply be False."""
        from app.routes_extras3 import _internal_token_ok
        monkeypatch.setenv("CORE_INTERNAL_TOKEN", "s3cret")
        assert _internal_token_ok(_internal_req("s3crét")) is False
        assert _internal_token_ok(_internal_req(b"s3cret\x80".decode("latin-1"))) is False
        # A non-ASCII secret on the expected side compares fine too.
        monkeypatch.setenv("CORE_INTERNAL_TOKEN", "süper")
        assert _internal_token_ok(_internal_req("süper")) is True
        assert _internal_token_ok(_internal_req("super")) is False

    @pytest.mark.asyncio
    async def test_deferred_fire_forbids_non_ascii_token(self, monkeypatch):
        monkeypatch.setenv("CORE_INTERNAL_TOKEN", "s3cret")
        app = _app_with_routes()
        handler = _find_route(app, "/internal/deferred/fire", "POST")
        with patch("app.deferred.fire", new=AsyncMock()) as fire:
            resp = await handler(_internal_req("s3crét"))
        assert resp.status_code == 403
        fire.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deferred_fire_runs_with_the_right_token(self, monkeypatch):
        monkeypatch.setenv("CORE_INTERNAL_TOKEN", "s3cret")
        app = _app_with_routes()
        handler = _find_route(app, "/internal/deferred/fire", "POST")
        with patch("app.deferred.fire", new=AsyncMock(return_value=True)) as fire:
            result = await handler(_internal_req("s3cret"))
        assert result == {"task_id": "t1", "fired": True}
        fire.assert_awaited_once_with("t1")

    @pytest.mark.asyncio
    async def test_her_pov_run_forbids_non_ascii_token(self, monkeypatch):
        monkeypatch.setenv("CORE_INTERNAL_TOKEN", "s3cret")
        app = _app_with_routes()
        handler = _find_route(app, "/internal/jobs/her-pov/run", "POST")
        with patch("app.memory_her_pov.run_job_from_queue", new=AsyncMock()) as run:
            resp = await handler(_internal_req(b"s3cret\x80".decode("latin-1")))
        assert resp.status_code == 403
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_her_pov_run_with_the_right_token(self, monkeypatch):
        monkeypatch.setenv("CORE_INTERNAL_TOKEN", "s3cret")
        app = _app_with_routes()
        handler = _find_route(app, "/internal/jobs/her-pov/run", "POST")
        with patch("app.memory_her_pov.run_job_from_queue",
                   new=AsyncMock(return_value=True)) as run:
            result = await handler(_internal_req("s3cret"))
        assert result == {"job_id": "j1", "ok": True}
        run.assert_awaited_once_with("j1")
