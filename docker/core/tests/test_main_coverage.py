"""Behavioral unit tests for app.main — app construction, lifespan, middleware.

These exercise the real functions/middleware with mocked I/O and assert
concrete behavior:
  * lifespan OPENS the pool on entry and CLOSES it on exit, starts + stops the
    proactive engine, and never schedules an LLM keepalive;
  * each middleware dispatch sets the headers / status it promises;
  * the rate-limit middleware skips unprotected paths, consumes a token on
    protected ones, and returns 429 on overflow;
  * run_migration propagates migration failures to startup;
  * generate_daily_recap honors the affection-tone branches and the
    too-short-conversation guard;
  * proactive_callback fans out to connected sockets, else falls back to push.

No real network/DB/sleep: init_pool/close_pool, Redis, the LLM router, and
asyncio.sleep are all patched.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.main as main
from app.extraction_llm import ExtractionLLMError, ExtractionUnavailable
from app.models import SessionState
from app.ws_manager import WSManager


# ── Request stub for BaseHTTPMiddleware.dispatch ─────────────────────────────


def _request(path: str = "/health", headers: dict | None = None,
             client_host: str = "1.2.3.4", routes: list | None = None):
    """Minimal stand-in for starlette Request used by the middlewares.

    `routes` populates request.app.routes for the metrics middleware's
    route-template lookup; default is a single route matching `path`.
    """
    req = MagicMock()
    req.url = SimpleNamespace(path=path)
    req.headers = headers or {}
    req.client = SimpleNamespace(host=client_host)
    req.state = SimpleNamespace()
    if routes is None:
        routes = [SimpleNamespace(path=path, path_regex=None)]
    req.app = SimpleNamespace(routes=routes)
    return req


def _response_factory(status_code: int = 200):
    """Return a fresh mock response with a real dict for headers each call."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {}
    return resp


# ── App object construction ──────────────────────────────────────────────────


class TestAppConstruction:
    def test_app_is_fastapi_with_title_and_lifespan(self):
        from fastapi import FastAPI
        assert isinstance(main.app, FastAPI)
        assert main.app.title == "Companion Core"
        # lifespan wired in (router has a lifespan context).
        assert main.app.router.lifespan_context is not None

    def test_security_and_tracing_middlewares_registered(self):
        names = {m.cls.__name__ for m in main.app.user_middleware}
        # Every custom middleware class is mounted on the app.
        for cls in ("_SecurityHeadersMiddleware", "_RequestIdMiddleware",
                    "_RateLimitMiddleware", "_MetricsMiddleware",
                    "CORSMiddleware"):
            assert cls in names, f"{cls} not mounted"

    def test_getattr_lazy_reexports_enhance_image_prompt(self):
        """__getattr__ lazily resolves _enhance_image_prompt from helpers."""
        # _enhance_image_prompt exists as a module global (=None), so normal
        # attribute access won't hit __getattr__. Call the hook directly to
        # exercise the lazy-reexport branch.
        fn = main.__getattr__("_enhance_image_prompt")
        assert callable(fn)

    def test_getattr_unknown_name_raises(self):
        with pytest.raises(AttributeError):
            main.__getattr__("totally_unknown_symbol")


# ── _SecurityHeadersMiddleware ───────────────────────────────────────────────


class TestSecurityHeadersMiddleware:
    async def test_sets_all_hardening_headers(self):
        mw = main._SecurityHeadersMiddleware(app=MagicMock())
        resp = _response_factory()
        call_next = AsyncMock(return_value=resp)
        out = await mw.dispatch(_request(), call_next)
        assert out.headers["X-Content-Type-Options"] == "nosniff"
        assert out.headers["X-Frame-Options"] == "DENY"
        assert out.headers["X-XSS-Protection"] == "1; mode=block"
        assert out.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "Content-Security-Policy" not in out.headers

    async def test_only_the_3d_stage_may_be_framed_and_only_by_klukai(self):
        mw = main._SecurityHeadersMiddleware(app=MagicMock())
        call_next = AsyncMock(return_value=_response_factory())
        out = await mw.dispatch(_request("/app/companion.html"), call_next)
        assert out.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert out.headers["Content-Security-Policy"] == "frame-ancestors 'self'"
        assert out.headers["X-Content-Type-Options"] == "nosniff"
        for path in ("/app/", "/app/index.html", "/app/login.html", "/api/messages"):
            call_next = AsyncMock(return_value=_response_factory())
            out = await mw.dispatch(_request(path), call_next)
            assert out.headers["X-Frame-Options"] == "DENY", path
            assert "Content-Security-Policy" not in out.headers, path


# ── _RequestIdMiddleware ─────────────────────────────────────────────────────


class TestRequestIdMiddleware:
    async def test_generates_request_id_when_absent(self):
        mw = main._RequestIdMiddleware(app=MagicMock())
        resp = _response_factory()
        req = _request(headers={})
        out = await mw.dispatch(req, AsyncMock(return_value=resp))
        # A 12-char id is generated, stashed on state, and echoed in the header.
        rid = req.state.request_id
        assert len(rid) == 12
        assert out.headers["X-Request-ID"] == rid

    async def test_propagates_client_supplied_request_id(self):
        mw = main._RequestIdMiddleware(app=MagicMock())
        resp = _response_factory()
        req = _request(headers={"X-Request-ID": "client-rid-42"})
        out = await mw.dispatch(req, AsyncMock(return_value=resp))
        assert req.state.request_id == "client-rid-42"
        assert out.headers["X-Request-ID"] == "client-rid-42"


# ── _RateLimitMiddleware ─────────────────────────────────────────────────────


class TestRateLimitMiddleware:
    async def test_unprotected_path_skips_limiter(self):
        """A path with no bucket bypasses Redis entirely — no headers added."""
        mw = main._RateLimitMiddleware(app=MagicMock())
        resp = _response_factory()
        call_next = AsyncMock(return_value=resp)
        with patch("app.rate_limit.consume") as cac:
            out = await mw.dispatch(_request(path="/health"), call_next)
        cac.assert_not_called()
        assert "X-RateLimit-Bucket" not in out.headers

    async def test_protected_path_consumes_token_and_sets_headers(self):
        mw = main._RateLimitMiddleware(app=MagicMock())
        resp = _response_factory()
        call_next = AsyncMock(return_value=resp)
        with patch("app.rate_limit.consume",
                   new=AsyncMock(return_value=(7, 0, None))) as cac:
            out = await mw.dispatch(
                _request(path="/api/auth/login"), call_next
            )
        cac.assert_awaited_once()
        # Pre-auth login resolves identity to the client IP.
        assert cac.await_args.args == ("1.2.3.4", "login")
        assert out.headers["X-RateLimit-Remaining"] == "7"
        assert out.headers["X-RateLimit-Bucket"] == "login"

    async def test_bad_bearer_token_falls_back_to_ip(self):
        """If token resolution raises, identity falls back to the client IP."""
        mw = main._RateLimitMiddleware(app=MagicMock())
        resp = _response_factory()
        with patch("app.auth.get_user_from_token",
                   new=AsyncMock(side_effect=RuntimeError("bad token"))), \
             patch("app.rate_limit.consume",
                   new=AsyncMock(return_value=(1, 0, None))) as cac:
            await mw.dispatch(
                _request(path="/api/user/export", client_host="9.9.9.9",
                         headers={"Authorization": "Bearer broken"}),
                AsyncMock(return_value=resp),
            )
        assert cac.await_args.args == ("9.9.9.9", "export")

    async def test_bearer_token_resolves_user_identity(self):
        mw = main._RateLimitMiddleware(app=MagicMock())
        resp = _response_factory()
        with patch("app.auth.get_user_from_token",
                   new=AsyncMock(return_value="alice")), \
             patch("app.rate_limit.consume",
                   new=AsyncMock(return_value=(3, 0, None))) as cac:
            await mw.dispatch(
                _request(path="/api/user/export",
                         headers={"Authorization": "Bearer good-token"}),
                AsyncMock(return_value=resp),
            )
        # Identity is the resolved user, not the IP.
        assert cac.await_args.args == ("alice", "export")

    async def test_overflow_returns_429_without_calling_handler(self):
        from app.rate_limit import RateLimitExceeded
        mw = main._RateLimitMiddleware(app=MagicMock())
        call_next = AsyncMock()
        exc = RateLimitExceeded("search", 30)
        with patch("app.rate_limit.consume",
                   new=AsyncMock(side_effect=exc)):
            out = await mw.dispatch(
                _request(path="/api/memories/search"), call_next
            )
        # Downstream handler is never reached; a 429 with Retry-After is returned.
        call_next.assert_not_awaited()
        assert out.status_code == 429
        assert out.headers["Retry-After"] == "30"

    async def test_409_from_the_handler_refunds_the_token(self):
        """The token is consumed before call_next; a 409 (GPU busy / already
        drawing) did none of the work the limit guards, so it goes back —
        else one 409 on the 1-per-10-min portrait bucket locks him out."""
        from app.rate_limit import Consumption

        receipt = Consumption("k", 1)
        mw = main._RateLimitMiddleware(app=MagicMock())
        resp = _response_factory(status_code=409)
        with patch("app.auth.get_user_from_token",
                   new=AsyncMock(return_value="alice")), \
             patch("app.rate_limit.consume",
                   new=AsyncMock(return_value=(0, 600, receipt))), \
             patch("app.rate_limit.refund",
                   new=AsyncMock(return_value=True)) as refund:
            out = await mw.dispatch(
                _request(path="/api/portrait/refresh",
                         headers={"Authorization": "Bearer good-token"}),
                AsyncMock(return_value=resp),
            )
        refund.assert_awaited_once_with(receipt)
        # The 409 itself passes through, with the refunded count advertised.
        assert out.status_code == 409
        assert out.headers["X-RateLimit-Remaining"] == "1"
        assert out.headers["X-RateLimit-Bucket"] == "portrait_refresh"

    @pytest.mark.parametrize("status", [200, 202, 400, 404, 500])
    async def test_other_statuses_keep_the_token(self, status):
        mw = main._RateLimitMiddleware(app=MagicMock())
        resp = _response_factory(status_code=status)
        with patch("app.rate_limit.consume",
                   new=AsyncMock(return_value=(4, 0, None))), \
             patch("app.rate_limit.refund", new=AsyncMock()) as refund:
            out = await mw.dispatch(_request(path="/api/portrait/refresh"),
                                    AsyncMock(return_value=resp))
        refund.assert_not_awaited()
        assert out.headers["X-RateLimit-Remaining"] == "4"

    async def test_a_409_without_a_refunded_token_advertises_no_extra(self):
        """A fail-open admission took no token: the 409 refunds nothing and
        the remaining count is not inflated."""
        mw = main._RateLimitMiddleware(app=MagicMock())
        resp = _response_factory(status_code=409)
        with patch("app.rate_limit.consume",
                   new=AsyncMock(return_value=(999, 0, None))), \
             patch("app.rate_limit.refund",
                   new=AsyncMock(return_value=False)) as refund:
            out = await mw.dispatch(_request(path="/api/portrait/refresh"),
                                    AsyncMock(return_value=resp))
        refund.assert_awaited_once_with(None)
        assert out.headers["X-RateLimit-Remaining"] == "999"


# ── _MetricsMiddleware ───────────────────────────────────────────────────────


class TestMetricsMiddleware:
    async def test_records_counter_and_latency_on_success(self):
        mw = main._MetricsMiddleware(app=MagicMock())
        resp = _response_factory(status_code=200)
        with patch("app.metrics.incr") as incr, \
             patch("app.metrics.observe_latency") as obs:
            out = await mw.dispatch(_request(path="/api/x"),
                                    AsyncMock(return_value=resp))
        assert out is resp
        assert incr.call_count == 2  # canonical RED plus legacy JSON counter
        assert incr.call_args.kwargs["status"] == "200"
        assert incr.call_args.kwargs["path"] == "/api/x"
        assert obs.call_count == 2  # canonical RED plus legacy JSON histogram

    async def test_records_500_and_reraises_on_handler_exception(self):
        mw = main._MetricsMiddleware(app=MagicMock())
        boom = RuntimeError("handler exploded")
        with patch("app.metrics.incr") as incr, \
             patch("app.metrics.observe_latency") as obs:
            with pytest.raises(RuntimeError):
                await mw.dispatch(_request(path="/api/y"),
                                  AsyncMock(side_effect=boom))
        # Even on failure, a 500 metric + latency are recorded before re-raise.
        assert incr.call_args.kwargs["status"] == "500"
        assert obs.call_count == 2  # canonical RED plus legacy JSON histogram

    async def test_unmatched_path_collapses_to_constant_label(self):
        """Scan/404 paths must not mint unbounded metric label values."""
        mw = main._MetricsMiddleware(app=MagicMock())
        resp = _response_factory(status_code=404)
        with patch("app.metrics.incr") as incr, \
             patch("app.metrics.observe_latency") as obs:
            await mw.dispatch(
                _request(path="/wp-admin/setup.php", routes=[]),
                AsyncMock(return_value=resp),
            )
        assert incr.call_args.kwargs["path"] == "_unmatched"
        assert obs.call_args.kwargs["path"] == "_unmatched"

    async def test_templated_route_label_uses_route_path(self):
        """Parameterised routes label by template, not per-id raw path."""
        import re
        route = SimpleNamespace(
            path="/api/memories/{memory_id}/image",
            path_regex=re.compile(r"^/api/memories/[^/]+/image$"),
        )
        mw = main._MetricsMiddleware(app=MagicMock())
        resp = _response_factory(status_code=200)
        with patch("app.metrics.incr") as incr, \
             patch("app.metrics.observe_latency"):
            await mw.dispatch(
                _request(path="/api/memories/abc123/image", routes=[route]),
                AsyncMock(return_value=resp),
            )
        assert incr.call_args.kwargs["path"] == "/api/memories/{memory_id}/image"


# ── Global exception handler ─────────────────────────────────────────────────


class TestGlobalExceptionHandler:
    async def test_returns_500_json_envelope(self):
        out = await main._global_exception_handler(
            _request(path="/api/boom"), RuntimeError("kaboom")
        )
        assert out.status_code == 500
        import json
        assert json.loads(bytes(out.body))["error"] == "Internal server error"


# ── run_migration ────────────────────────────────────────────────────────────


class TestRunMigration:
    async def test_failing_migration_prevents_startup(self, tmp_path):
        (tmp_path / "001.sql").write_text("BAD SQL;")
        conn = MagicMock()
        conn.execute = AsyncMock(side_effect=RuntimeError("database failure"))
        conn.transaction.return_value.__aenter__ = AsyncMock()
        conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.connection.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.connection.return_value.__aexit__ = AsyncMock(return_value=False)
        fake_path = MagicMock()
        fake_path.parent.parent.__truediv__ = MagicMock(return_value=tmp_path)
        with patch.object(main, "Path", return_value=fake_path), \
             patch.object(main, "get_pool", return_value=pool), \
             pytest.raises(RuntimeError, match="database failure"):
            await main.run_migration()


# ── generate_daily_recap ─────────────────────────────────────────────────────


def _recap_pool(rows):
    """Build a mock pool whose connection().execute().fetchall() -> rows."""
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(return_value=rows)
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=cursor)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.connection = MagicMock(return_value=cm)
    return pool


def _haiku(reply):
    """Patch the Haiku transport: a string is its reply, an exception its failure."""
    if isinstance(reply, BaseException):
        return patch("app.extraction_llm.complete", new=AsyncMock(side_effect=reply))
    return patch("app.extraction_llm.complete", new=AsyncMock(return_value=reply))


class TestGenerateDailyRecap:
    async def test_returns_none_when_too_few_messages(self):
        pool = _recap_pool([("user", "hi"), ("assistant", "hello")])  # < 4 rows
        with patch.object(main, "get_pool", return_value=pool), \
             _haiku("unused") as haiku:
            out = await main.generate_daily_recap(affection_level=5)
        assert out is None
        haiku.assert_not_awaited()

    async def test_recap_text_comes_from_haiku(self):
        from app.background_llm import haiku_budget

        rows = [("user", f"msg {i}") for i in range(8)]
        pool = _recap_pool(rows)
        with patch.object(main, "get_pool", return_value=pool), \
             _haiku("Today we talked.") as haiku:
            out = await main.generate_daily_recap(affection_level=5)
        assert out == "Today we talked."
        prompt = haiku.await_args.args[0]
        assert prompt.startswith("You are Klukai writing a brief evening operational log")
        assert "Commander: msg 7" in prompt
        assert haiku.await_args.kwargs == {
            "max_tokens": haiku_budget(2048),
            "route": "daily_recap",
        }

    @pytest.mark.parametrize(
        ("affection_level", "tone"),
        [(5, "Write warmly"), (1, "professionally"), (0, "coldly")],
    )
    async def test_tone_follows_affection(self, affection_level, tone):
        rows = [("user", f"m {i}") for i in range(8)]
        pool = _recap_pool(rows)
        with patch.object(main, "get_pool", return_value=pool), _haiku("ok") as haiku:
            await main.generate_daily_recap(affection_level=affection_level)
        assert tone in haiku.await_args.args[0]

    async def test_recap_swallows_db_error_returns_none(self):
        with patch.object(main, "get_pool", side_effect=RuntimeError("db down")):
            out = await main.generate_daily_recap(affection_level=5)
        assert out is None

    @pytest.mark.parametrize(
        "reply",
        [ExtractionUnavailable("cap"), ExtractionLLMError("boom"), "", "   "],
    )
    async def test_no_recap_without_a_haiku_reply(self, reply):
        """Haiku capped, failed or blank: no recap tonight. Nothing canned is
        pushed as her words and no local model is tried."""
        rows = [("user", f"m {i}") for i in range(8)]
        pool = _recap_pool(rows)
        with patch.object(main, "get_pool", return_value=pool), _haiku(reply) as haiku:
            out = await main.generate_daily_recap(affection_level=5)
        assert out is None
        haiku.assert_awaited_once()

    async def test_recap_window_is_the_commanders_local_day(self):
        """`today` is the Commander's date and the SQL buckets created_at in
        his zone. With the container's UTC date and a UTC `created_at::date`
        the 21:00 Chicago run saw only messages after 19:00 local — rarely
        the four it needs — so the recap silently never fired."""
        from datetime import date, datetime

        rows = [("user", f"m {i}") for i in range(8)]
        pool = _recap_pool(rows)
        conn = pool.connection.return_value.__aenter__.return_value
        # 21:00 Chicago on the 11th is already 02:00Z on the 12th.
        local_evening = datetime(2026, 10, 11, 21, 0)

        with patch.object(main, "get_pool", return_value=pool), \
             patch("app.proactive.state.now_local", return_value=local_evening), \
             _haiku("Today we talked."):
            out = await main.generate_daily_recap(affection_level=5)

        assert out == "Today we talked."
        sql, params = conn.execute.await_args.args
        assert "AT TIME ZONE" in sql
        assert "created_at::date" not in sql
        assert params == ("jalsarraf", "America/Chicago", date(2026, 10, 11))
        # Her own check-ins are still excluded from the recap source.
        assert "<> 'proactive'" in sql


# ── proactive_callback ───────────────────────────────────────────────────────


class TestProactiveCallback:
    """Scheduled proactivity is single-user: the line is persisted for the
    Commander first (send_proactive writes the row even with no device
    connected), then delivered live or by push. The return value tells the
    engine whether anyone actually received it."""

    async def test_persists_then_delivers_live_when_connected(self):
        from app.decisions import PRIMARY_USER

        ws = MagicMock()
        ws.is_connected = MagicMock(return_value=True)
        ws.send_proactive = AsyncMock()
        with patch.object(main, "ws", ws), \
             patch.object(main, "send_push", new=AsyncMock(return_value=0)) as push:
            delivered = await main.proactive_callback("Mission update.")
        assert delivered is True
        ws.send_proactive.assert_awaited_once_with(PRIMARY_USER, "Mission update.")
        push.assert_not_awaited()  # delivered over WS, no push fallback

    async def test_offline_still_persists_and_pushes(self):
        from app.decisions import PRIMARY_USER

        ws = MagicMock()
        ws.is_connected = MagicMock(return_value=False)
        ws.send_proactive = AsyncMock()
        with patch.object(main, "ws", ws), \
             patch.object(main, "send_push", new=AsyncMock(return_value=1)) as push:
            delivered = await main.proactive_callback("Ping.")
        assert delivered is True
        # History gets the line even though no device was listening.
        ws.send_proactive.assert_awaited_once_with(PRIMARY_USER, "Ping.")
        push.assert_awaited_once()
        assert push.await_args.kwargs["body"] == "Ping."

    async def test_offline_with_no_push_recipient_is_not_delivered(self):
        """Nobody connected and the push reached no subscriber: the engine
        must not mark a promise follow-up as sent, so this is False."""
        ws = MagicMock()
        ws.is_connected = MagicMock(return_value=False)
        ws.send_proactive = AsyncMock()
        with patch.object(main, "ws", ws), \
             patch.object(main, "send_push", new=AsyncMock(return_value=0)):
            delivered = await main.proactive_callback("Ping.")
        assert delivered is False
        ws.send_proactive.assert_awaited_once()  # still in history


# ── lifespan ─────────────────────────────────────────────────────────────────


@pytest.fixture
def lifespan_mocks(monkeypatch):
    """Patch every lifespan collaborator. Returns the key mocks for assertions.

    Task creation records real tasks so lifespan must cancel and join them;
    no application services or provider calls run.
    """
    init_pool = AsyncMock()
    close_pool = AsyncMock()
    monkeypatch.setattr(main, "init_pool", init_pool)
    monkeypatch.setattr(main, "close_pool", close_pool)
    monkeypatch.setattr(main, "run_migration", AsyncMock())

    memory = MagicMock()
    memory.init = AsyncMock()
    memory.close = AsyncMock()
    memory.get_session = AsyncMock(return_value=None)
    router = MagicMock()
    router.init = AsyncMock()
    router.close = AsyncMock()
    router.keepalive = AsyncMock()
    mcp = MagicMock()
    mcp.init = AsyncMock()
    mcp.close = AsyncMock()
    affection = MagicMock()
    affection.init = AsyncMock()
    affection.close = AsyncMock()
    proactive = MagicMock()
    proactive.set_callback = MagicMock()
    proactive.set_recap_callback = MagicMock()
    proactive.set_session_getter = MagicMock()
    proactive.start = MagicMock()
    proactive.stop = MagicMock()
    proactive.close = AsyncMock(side_effect=proactive.stop)

    monkeypatch.setattr(main, "memory", memory)
    monkeypatch.setattr(main, "router", router)
    monkeypatch.setattr(main, "mcp", mcp)
    monkeypatch.setattr(main, "affection", affection)
    monkeypatch.setattr(main, "proactive", proactive)
    monkeypatch.setattr(main, "events_init", AsyncMock())
    monkeypatch.setattr(main, "events_close", AsyncMock())
    monkeypatch.setattr(main, "load_personality", MagicMock())

    # auth.init_users + cleanup are imported lazily inside lifespan.
    monkeypatch.setattr("app.auth.init_users", AsyncMock(), raising=False)
    monkeypatch.setattr("app.auth.cleanup_expired_sessions", AsyncMock(),
                        raising=False)

    # Run real owned tasks so shutdown cancellation/join is exercised.
    created = []

    def fake_create_task(coro, **kwargs):
        task = asyncio.Task(coro, **kwargs)
        created.append(task)
        return task

    monkeypatch.setattr(main.asyncio, "create_task", fake_create_task)

    return SimpleNamespace(
        init_pool=init_pool, close_pool=close_pool, memory=memory,
        router=router, mcp=mcp, affection=affection, proactive=proactive,
        created=created,
    )


class TestLifespan:
    async def test_startup_opens_pool_and_inits_services(self, lifespan_mocks, monkeypatch):
        monkeypatch.delenv("KLUKAI_LLM_KEEPALIVE", raising=False)
        async with main.lifespan(main.app):
            # Inside the context: startup ran.
            lifespan_mocks.init_pool.assert_awaited_once_with(min_size=2, max_size=10)
            lifespan_mocks.memory.init.assert_awaited_once()
            lifespan_mocks.router.init.assert_awaited_once()
            lifespan_mocks.affection.init.assert_awaited_once()
            lifespan_mocks.proactive.start.assert_called_once()
            # Pool not yet closed while the app is "running".
            lifespan_mocks.close_pool.assert_not_awaited()

    async def test_shutdown_closes_pool_and_stops_proactive(self, lifespan_mocks, monkeypatch):
        monkeypatch.delenv("KLUKAI_LLM_KEEPALIVE", raising=False)
        async with main.lifespan(main.app):
            pass
        # After exit: teardown ran in order.
        lifespan_mocks.close_pool.assert_awaited_once()
        lifespan_mocks.proactive.stop.assert_called_once()
        lifespan_mocks.memory.close.assert_awaited_once()
        lifespan_mocks.router.close.assert_awaited_once()
        lifespan_mocks.affection.close.assert_awaited_once()

    async def test_callbacks_registered_on_startup(self, lifespan_mocks, monkeypatch):
        monkeypatch.delenv("KLUKAI_LLM_KEEPALIVE", raising=False)
        async with main.lifespan(main.app):
            pass
        lifespan_mocks.proactive.set_callback.assert_called_once_with(
            main.proactive_callback
        )
        lifespan_mocks.proactive.set_recap_callback.assert_called_once_with(
            main.generate_daily_recap
        )
        lifespan_mocks.proactive.set_session_getter.assert_called_once()
        lifespan_mocks.proactive.set_game_active_probe.assert_called_once_with(
            lifespan_mocks.router.is_game_active
        )

    async def test_session_cleanup_task_scheduled(self, lifespan_mocks, monkeypatch):
        monkeypatch.delenv("KLUKAI_LLM_KEEPALIVE", raising=False)
        async with main.lifespan(main.app):
            pass
        # At least the session-cleanup loop coroutine was handed to create_task.
        assert len(lifespan_mocks.created) >= 1

    async def test_keepalive_env_is_ignored(self, lifespan_mocks, monkeypatch):
        monkeypatch.setenv("KLUKAI_LLM_KEEPALIVE", "1")
        async with main.lifespan(main.app):
            pass
        lifespan_mocks.router.keepalive.assert_not_awaited()
        assert [task.get_name() for task in lifespan_mocks.created].count("session-cleanup") == 1
        assert all(task.done() for task in lifespan_mocks.created)

    async def test_keepalive_task_is_never_created(self, lifespan_mocks, monkeypatch):
        monkeypatch.setenv("KLUKAI_LLM_KEEPALIVE", "1")
        async with main.lifespan(main.app):
            pass
        lifespan_mocks.router.keepalive.assert_not_awaited()
        assert [task.get_name() for task in lifespan_mocks.created].count("session-cleanup") == 1
        assert all(task.done() for task in lifespan_mocks.created)

    async def test_session_getter_falls_back_to_primary_user(self, lifespan_mocks, monkeypatch):
        """The lifespan-local session getter tries connected users, then primary."""
        monkeypatch.delenv("KLUKAI_LLM_KEEPALIVE", raising=False)
        captured = {}
        lifespan_mocks.proactive.set_session_getter.side_effect = (
            lambda fn: captured.setdefault("getter", fn)
        )
        ws = WSManager()
        monkeypatch.setattr(main, "ws", ws)
        primary = SessionState(conversation_id="c")
        lifespan_mocks.memory.get_session = AsyncMock(return_value=primary)

        async with main.lifespan(main.app):
            getter = captured["getter"]
            result = await getter()  # no connected users -> primary fallback
        assert result is primary
        # The fallback queried the primary user's session key.
        assert lifespan_mocks.memory.get_session.await_args.args[0] == "session:jalsarraf"

    async def test_session_getter_returns_specific_user_when_found(self, lifespan_mocks, monkeypatch):
        """When a user_id is given and has a session, return it immediately."""
        monkeypatch.delenv("KLUKAI_LLM_KEEPALIVE", raising=False)
        captured = {}
        lifespan_mocks.proactive.set_session_getter.side_effect = (
            lambda fn: captured.setdefault("getter", fn)
        )
        ws = WSManager()
        monkeypatch.setattr(main, "ws", ws)
        wanted = SessionState(conversation_id="bob-conv")
        lifespan_mocks.memory.get_session = AsyncMock(return_value=wanted)

        async with main.lifespan(main.app):
            result = await captured["getter"]("bob")
        assert result is wanted
        assert lifespan_mocks.memory.get_session.await_args.args[0] == "session:bob"

    async def test_session_getter_defaults_to_primary_despite_other_connections(self, lifespan_mocks, monkeypatch):
        """Connected users never supply private context to the primary schedule."""
        monkeypatch.delenv("KLUKAI_LLM_KEEPALIVE", raising=False)
        captured = {}
        lifespan_mocks.proactive.set_session_getter.side_effect = (
            lambda fn: captured.setdefault("getter", fn)
        )
        ws = WSManager()
        socket = MagicMock(close=AsyncMock())
        ws._connections = {"carol": {socket}}
        monkeypatch.setattr(main, "ws", ws)
        primary_session = SessionState(conversation_id="primary-conv")
        lifespan_mocks.memory.get_session = AsyncMock(return_value=primary_session)

        async with main.lifespan(main.app):
            result = await captured["getter"]()  # no user_id -> primary owner
        assert result is primary_session
        assert lifespan_mocks.memory.get_session.await_args.args[0] == "session:jalsarraf"

    async def test_keepalive_override_cannot_trigger_warmup(self, lifespan_mocks, monkeypatch):
        monkeypatch.setenv("KLUKAI_LLM_KEEPALIVE", "1")
        lifespan_mocks.router.keepalive = AsyncMock(side_effect=RuntimeError("warmup boom"))
        async with main.lifespan(main.app):
            pass
        lifespan_mocks.router.keepalive.assert_not_awaited()
        lifespan_mocks.close_pool.assert_awaited_once()

    @pytest.mark.parametrize("fail", [False, True])
    async def test_session_cleanup_loop_retries_after_errors(self, monkeypatch, fail):
        cleanup = AsyncMock(side_effect=RuntimeError("db blip") if fail else None)
        monkeypatch.setattr("app.auth.cleanup_expired_sessions", cleanup)
        calls = 0
        async def sleep_then_cancel(_secs):
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise asyncio.CancelledError
        with patch("app.main.asyncio.sleep", new=sleep_then_cancel):
            with pytest.raises(asyncio.CancelledError):
                await main._session_cleanup_loop()
        cleanup.assert_awaited_once()


@pytest.mark.parametrize('cancelled', [False, True])
async def test_lifespan_owns_handoff_before_closing_dependencies(lifespan_mocks, monkeypatch, cancelled):
    from contextlib import asynccontextmanager
    from app import job_handoff
    order = []

    @asynccontextmanager
    async def running():
        order.append('start')
        try:
            yield
        finally:
            order.append('stop')

    async def close_events():
        order.append('events_closed')

    monkeypatch.setattr(job_handoff, 'running', running)
    monkeypatch.setattr(main, 'events_close', close_events)
    async def serve():
        async with main.lifespan(main.app):
            assert order == ['start']
            if cancelled:
                raise asyncio.CancelledError
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await serve()
    else:
        await serve()
    assert order == ['start', 'stop', 'events_closed']
    lifespan_mocks.close_pool.assert_awaited_once()


def _ordered_pool(rows):
    """A pool that honours the query's ORDER BY created_at ASC|DESC LIMIT n
    over ``rows`` (oldest first), like Postgres would."""
    import re as _re

    async def execute(sql, params=None):
        found = _re.search(r"ORDER BY created_at (ASC|DESC) LIMIT (\d+)", sql)
        ordered = list(rows)
        if found and found.group(1) == "DESC":
            ordered.reverse()
        if found:
            ordered = ordered[: int(found.group(2))]
        cursor = MagicMock()
        cursor.fetchall = AsyncMock(return_value=ordered)
        return cursor

    conn = MagicMock()
    conn.execute = execute
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.connection = MagicMock(return_value=cm)
    return pool


class TestDailyRecapWindow:
    async def test_a_busy_day_recaps_its_latest_messages_in_order(self):
        """With more than 40 messages the evening log must cover the end of
        the day, not messages 21-40 of the morning."""
        rows = [("user", f"msg {i:02d}") for i in range(60)]
        text = AsyncMock(return_value="Today.")
        with patch.object(main, "get_pool", return_value=_ordered_pool(rows)), \
             patch("app.background_llm.text", new=text):
            assert await main.generate_daily_recap(affection_level=5) == "Today."
        prompt = text.await_args.args[1]
        assert "msg 59" in prompt and "msg 40" in prompt
        assert "msg 39" not in prompt
        assert prompt.index("msg 40") < prompt.index("msg 59")
