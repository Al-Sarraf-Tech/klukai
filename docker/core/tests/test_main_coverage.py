"""Behavioral unit tests for app.main — app construction, lifespan, middleware.

These exercise the real functions/middleware with mocked I/O and assert
concrete behavior:
  * lifespan OPENS the pool on entry and CLOSES it on exit, starts + stops the
    proactive engine, and cancels the keepalive task when one was started;
  * each middleware dispatch sets the headers / status it promises;
  * the rate-limit middleware skips unprotected paths, consumes a token on
    protected ones, and returns 429 on overflow;
  * run_migration applies every .sql file and survives a failing one;
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
from app.models import SessionState


# ── Request stub for BaseHTTPMiddleware.dispatch ─────────────────────────────


def _request(path: str = "/health", headers: dict | None = None,
             client_host: str = "1.2.3.4"):
    """Minimal stand-in for starlette Request used by the middlewares."""
    req = MagicMock()
    req.url = SimpleNamespace(path=path)
    req.headers = headers or {}
    req.client = SimpleNamespace(host=client_host)
    req.state = SimpleNamespace()
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
        with patch("app.rate_limit.check_and_consume") as cac:
            out = await mw.dispatch(_request(path="/health"), call_next)
        cac.assert_not_called()
        assert "X-RateLimit-Bucket" not in out.headers

    async def test_protected_path_consumes_token_and_sets_headers(self):
        mw = main._RateLimitMiddleware(app=MagicMock())
        resp = _response_factory()
        call_next = AsyncMock(return_value=resp)
        with patch("app.rate_limit.check_and_consume",
                   new=AsyncMock(return_value=(7, 0))) as cac:
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
             patch("app.rate_limit.check_and_consume",
                   new=AsyncMock(return_value=(1, 0))) as cac:
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
             patch("app.rate_limit.check_and_consume",
                   new=AsyncMock(return_value=(3, 0))) as cac:
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
        with patch("app.rate_limit.check_and_consume",
                   new=AsyncMock(side_effect=exc)):
            out = await mw.dispatch(
                _request(path="/api/memories/search"), call_next
            )
        # Downstream handler is never reached; a 429 with Retry-After is returned.
        call_next.assert_not_awaited()
        assert out.status_code == 429
        assert out.headers["Retry-After"] == "30"


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
        incr.assert_called_once()
        assert incr.call_args.kwargs["status"] == "200"
        assert incr.call_args.kwargs["path"] == "/api/x"
        obs.assert_called_once()

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
        obs.assert_called_once()


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
    async def test_applies_every_sql_file(self, tmp_path):
        # Two fake migration files.
        (tmp_path / "001_init.sql").write_text("CREATE TABLE a();")
        (tmp_path / "002_more.sql").write_text("CREATE TABLE b();")

        conn = AsyncMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.connection = MagicMock(return_value=cm)

        # Point Path(__file__).parent.parent / "migrations" at tmp_path by
        # patching Path inside main to return our dir for the glob.
        fake_path = MagicMock()
        fake_path.parent.parent.__truediv__ = MagicMock(return_value=tmp_path)
        with patch.object(main, "Path", return_value=fake_path), \
             patch.object(main, "get_pool", return_value=pool):
            await main.run_migration()

        # One execute + commit per migration file (sorted order).
        assert conn.execute.await_count == 2
        assert conn.commit.await_count == 2

    async def test_failing_migration_is_swallowed(self, tmp_path):
        (tmp_path / "001.sql").write_text("BAD SQL;")
        conn = AsyncMock()
        conn.execute = AsyncMock(side_effect=RuntimeError("already applied"))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.connection = MagicMock(return_value=cm)

        fake_path = MagicMock()
        fake_path.parent.parent.__truediv__ = MagicMock(return_value=tmp_path)
        with patch.object(main, "Path", return_value=fake_path), \
             patch.object(main, "get_pool", return_value=pool):
            await main.run_migration()  # must not raise
        # The failing migration was attempted (proving the except path ran) and
        # the broken SQL was never committed.
        conn.execute.assert_awaited_once()
        conn.commit.assert_not_awaited()


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


class TestGenerateDailyRecap:
    async def test_returns_none_when_too_few_messages(self):
        pool = _recap_pool([("user", "hi"), ("assistant", "hello")])  # < 4 rows
        with patch.object(main, "get_pool", return_value=pool):
            out = await main.generate_daily_recap(affection_level=5)
        assert out is None

    async def test_streams_recap_text_from_router(self):
        rows = [("user", f"msg {i}") for i in range(8)]
        pool = _recap_pool(rows)

        async def fake_stream(*a, **k):
            for tok in ["Today ", "we ", "talked."]:
                yield tok

        with patch.object(main, "get_pool", return_value=pool), \
             patch("app.context.router.route", new=AsyncMock(return_value={})), \
             patch("app.context.router.stream", new=fake_stream):
            out = await main.generate_daily_recap(affection_level=5)
        assert out == "Today we talked."

    async def test_warm_tone_for_high_affection(self):
        rows = [("user", f"m {i}") for i in range(8)]
        pool = _recap_pool(rows)
        captured = {}

        async def fake_stream(prompt, messages, config):
            captured["prompt"] = prompt
            yield "ok"

        with patch.object(main, "get_pool", return_value=pool), \
             patch("app.context.router.route", new=AsyncMock(return_value={})), \
             patch("app.context.router.stream", new=fake_stream):
            await main.generate_daily_recap(affection_level=5)
        assert "Write warmly" in captured["prompt"]

    async def test_professional_tone_for_mid_affection(self):
        rows = [("user", f"m {i}") for i in range(8)]
        pool = _recap_pool(rows)
        captured = {}

        async def fake_stream(prompt, messages, config):
            captured["prompt"] = prompt
            yield "ok"

        with patch.object(main, "get_pool", return_value=pool), \
             patch("app.context.router.route", new=AsyncMock(return_value={})), \
             patch("app.context.router.stream", new=fake_stream):
            await main.generate_daily_recap(affection_level=1)
        assert "professionally" in captured["prompt"]

    async def test_cold_tone_for_zero_affection(self):
        rows = [("user", f"m {i}") for i in range(8)]
        pool = _recap_pool(rows)
        captured = {}

        async def fake_stream(prompt, messages, config):
            captured["prompt"] = prompt
            yield "ok"

        with patch.object(main, "get_pool", return_value=pool), \
             patch("app.context.router.route", new=AsyncMock(return_value={})), \
             patch("app.context.router.stream", new=fake_stream):
            await main.generate_daily_recap(affection_level=0)
        assert "coldly" in captured["prompt"]

    async def test_recap_swallows_db_error_returns_none(self):
        with patch.object(main, "get_pool", side_effect=RuntimeError("db down")):
            out = await main.generate_daily_recap(affection_level=5)
        assert out is None


# ── proactive_callback ───────────────────────────────────────────────────────


class TestProactiveCallback:
    async def test_delivers_to_connected_sockets(self):
        ws = MagicMock()
        ws._connections = {"u1": object(), "u2": object()}
        ws.is_connected = MagicMock(return_value=True)
        ws.send_proactive = AsyncMock()
        with patch.object(main, "ws", ws), \
             patch.object(main, "send_push", new=AsyncMock()) as push:
            await main.proactive_callback("Mission update.")
        assert ws.send_proactive.await_count == 2
        push.assert_not_awaited()  # delivered over WS, no push fallback

    async def test_falls_back_to_push_when_no_one_connected(self):
        ws = MagicMock()
        ws._connections = {}
        ws.is_connected = MagicMock(return_value=False)
        ws.send_proactive = AsyncMock()
        with patch.object(main, "ws", ws), \
             patch.object(main, "send_push", new=AsyncMock()) as push:
            await main.proactive_callback("Ping.")
        ws.send_proactive.assert_not_awaited()
        push.assert_awaited_once()
        assert push.await_args.kwargs["body"] == "Ping."


# ── _keepalive_loop ──────────────────────────────────────────────────────────


class TestKeepaliveLoop:
    async def test_one_iteration_pings_router_then_stops(self):
        """Drive the infinite loop exactly once.

        The loop sleeps FIRST, then pings. So the 1st sleep must return, the
        ping fires, then the 2nd sleep raises CancelledError to break out —
        proving exactly one keepalive happened per cycle.
        """
        calls = {"n": 0}

        async def sleep_then_cancel(_secs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise asyncio.CancelledError

        router = MagicMock()
        router.keepalive = AsyncMock()
        with patch("app.main.asyncio.sleep", new=sleep_then_cancel), \
             patch("app.llm_router._KEEPALIVE_INTERVAL", 1), \
             patch.object(main, "router", router):
            with pytest.raises(asyncio.CancelledError):
                await main._keepalive_loop()
        router.keepalive.assert_awaited_once()

    async def test_keepalive_error_is_logged_not_raised(self):
        """A keepalive failure is swallowed; loop continues to next sleep."""
        seq = {"n": 0}

        async def sleep_twice(_secs):
            seq["n"] += 1
            if seq["n"] >= 2:
                raise asyncio.CancelledError

        router = MagicMock()
        router.keepalive = AsyncMock(side_effect=RuntimeError("LM down"))
        with patch("app.main.asyncio.sleep", new=sleep_twice), \
             patch("app.llm_router._KEEPALIVE_INTERVAL", 1), \
             patch.object(main, "router", router):
            with pytest.raises(asyncio.CancelledError):
                await main._keepalive_loop()
        # Tried at least once and did not propagate the RuntimeError.
        assert router.keepalive.await_count >= 1


# ── lifespan ─────────────────────────────────────────────────────────────────


@pytest.fixture
def lifespan_mocks(monkeypatch):
    """Patch every lifespan collaborator. Returns the key mocks for assertions.

    asyncio.create_task is replaced with a synchronous stub that records the
    coroutine and closes it (so the never-awaited session-cleanup loop doesn't
    warn or run).
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

    # create_task: record + immediately close the coro (don't schedule it).
    created = []

    def fake_create_task(coro):
        created.append(coro)
        coro.close()
        return MagicMock(spec=asyncio.Task)

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

    async def test_session_cleanup_task_scheduled(self, lifespan_mocks, monkeypatch):
        monkeypatch.delenv("KLUKAI_LLM_KEEPALIVE", raising=False)
        async with main.lifespan(main.app):
            pass
        # At least the session-cleanup loop coroutine was handed to create_task.
        assert len(lifespan_mocks.created) >= 1

    async def test_keepalive_enabled_when_env_set(self, lifespan_mocks, monkeypatch):
        monkeypatch.setenv("KLUKAI_LLM_KEEPALIVE", "1")
        async with main.lifespan(main.app):
            pass
        # Warmup ping fired and the keepalive task was created (2 create_task:
        # keepalive loop + session cleanup).
        lifespan_mocks.router.keepalive.assert_awaited()
        assert len(lifespan_mocks.created) >= 2

    async def test_keepalive_task_cancelled_on_shutdown(self, lifespan_mocks, monkeypatch):
        monkeypatch.setenv("KLUKAI_LLM_KEEPALIVE", "1")
        # Use a real cancellable task object so .cancel() is observable.
        cancel_flag = {"cancelled": False}

        class _FakeTask:
            def cancel(self):
                cancel_flag["cancelled"] = True

        def fake_create_task(coro):
            coro.close()
            return _FakeTask()

        monkeypatch.setattr(main.asyncio, "create_task", fake_create_task)
        async with main.lifespan(main.app):
            pass
        assert cancel_flag["cancelled"] is True

    async def test_session_getter_falls_back_to_primary_user(self, lifespan_mocks, monkeypatch):
        """The lifespan-local session getter tries connected users, then primary."""
        monkeypatch.delenv("KLUKAI_LLM_KEEPALIVE", raising=False)
        captured = {}
        lifespan_mocks.proactive.set_session_getter.side_effect = (
            lambda fn: captured.setdefault("getter", fn)
        )
        ws = MagicMock()
        ws._connections = {}
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
        ws = MagicMock()
        ws._connections = {}
        monkeypatch.setattr(main, "ws", ws)
        wanted = SessionState(conversation_id="bob-conv")
        lifespan_mocks.memory.get_session = AsyncMock(return_value=wanted)

        async with main.lifespan(main.app):
            result = await captured["getter"]("bob")
        assert result is wanted
        assert lifespan_mocks.memory.get_session.await_args.args[0] == "session:bob"

    async def test_session_getter_scans_connected_users(self, lifespan_mocks, monkeypatch):
        """With no user_id, the getter walks connected sockets and returns a hit."""
        monkeypatch.delenv("KLUKAI_LLM_KEEPALIVE", raising=False)
        captured = {}
        lifespan_mocks.proactive.set_session_getter.side_effect = (
            lambda fn: captured.setdefault("getter", fn)
        )
        ws = MagicMock()
        ws._connections = {"carol": object()}
        monkeypatch.setattr(main, "ws", ws)
        carol_session = SessionState(conversation_id="carol-conv")
        lifespan_mocks.memory.get_session = AsyncMock(return_value=carol_session)

        async with main.lifespan(main.app):
            result = await captured["getter"]()  # no user_id -> scan connections
        assert result is carol_session
        assert lifespan_mocks.memory.get_session.await_args.args[0] == "session:carol"

    async def test_keepalive_warmup_failure_is_swallowed(self, lifespan_mocks, monkeypatch):
        """A warmup-ping failure during startup must not crash the lifespan."""
        monkeypatch.setenv("KLUKAI_LLM_KEEPALIVE", "1")
        lifespan_mocks.router.keepalive = AsyncMock(side_effect=RuntimeError("warmup boom"))
        # Startup completes despite the warmup error.
        async with main.lifespan(main.app):
            pass
        lifespan_mocks.router.keepalive.assert_awaited()
        lifespan_mocks.close_pool.assert_awaited_once()  # still shut down cleanly

    async def test_session_cleanup_loop_calls_cleanup(self, monkeypatch):
        """Drive the inner _session_cleanup_loop one iteration via patched sleep.

        We capture the coroutine handed to create_task instead of closing it,
        then run it once with a sleep that returns then raises CancelledError.
        """
        # Patch all startup collaborators minimally.
        for name in ("init_pool", "close_pool", "run_migration", "events_init",
                     "events_close"):
            monkeypatch.setattr(main, name, AsyncMock())
        for name in ("memory", "router", "mcp", "affection"):
            obj = MagicMock()
            obj.init = AsyncMock()
            obj.close = AsyncMock()
            obj.keepalive = AsyncMock()
            obj.get_session = AsyncMock(return_value=None)
            monkeypatch.setattr(main, name, obj)
        proactive = MagicMock()
        for m in ("set_callback", "set_recap_callback", "set_session_getter",
                  "start", "stop"):
            setattr(proactive, m, MagicMock())
        monkeypatch.setattr(main, "proactive", proactive)
        monkeypatch.setattr(main, "load_personality", MagicMock())
        monkeypatch.setattr("app.auth.init_users", AsyncMock(), raising=False)
        cleanup = AsyncMock()
        monkeypatch.setattr("app.auth.cleanup_expired_sessions", cleanup, raising=False)
        monkeypatch.delenv("KLUKAI_LLM_KEEPALIVE", raising=False)

        captured = []
        monkeypatch.setattr(main.asyncio, "create_task",
                            lambda coro: captured.append(coro) or MagicMock())

        calls = {"n": 0}

        async def sleep_then_cancel(_secs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise asyncio.CancelledError

        async with main.lifespan(main.app):
            pass

        # Run the captured cleanup-loop coroutine one iteration.
        assert captured, "session cleanup loop was not scheduled"
        with patch("app.main.asyncio.sleep", new=sleep_then_cancel):
            with pytest.raises(asyncio.CancelledError):
                await captured[-1]
        cleanup.assert_awaited()

    async def test_session_cleanup_loop_survives_cleanup_error(self, monkeypatch):
        """A failing cleanup must be swallowed so the 6h janitor keeps running."""
        for name in ("init_pool", "close_pool", "run_migration", "events_init",
                     "events_close"):
            monkeypatch.setattr(main, name, AsyncMock())
        for name in ("memory", "router", "mcp", "affection"):
            obj = MagicMock()
            obj.init = AsyncMock()
            obj.close = AsyncMock()
            obj.keepalive = AsyncMock()
            obj.get_session = AsyncMock(return_value=None)
            monkeypatch.setattr(main, name, obj)
        proactive = MagicMock()
        for m in ("set_callback", "set_recap_callback", "set_session_getter",
                  "start", "stop"):
            setattr(proactive, m, MagicMock())
        monkeypatch.setattr(main, "proactive", proactive)
        monkeypatch.setattr(main, "load_personality", MagicMock())
        monkeypatch.setattr("app.auth.init_users", AsyncMock(), raising=False)
        # cleanup raises — the loop's except: pass must absorb it.
        monkeypatch.setattr("app.auth.cleanup_expired_sessions",
                            AsyncMock(side_effect=RuntimeError("db blip")),
                            raising=False)
        monkeypatch.delenv("KLUKAI_LLM_KEEPALIVE", raising=False)

        captured = []
        monkeypatch.setattr(main.asyncio, "create_task",
                            lambda coro: captured.append(coro) or MagicMock())

        calls = {"n": 0}

        async def sleep_then_cancel(_secs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise asyncio.CancelledError

        async with main.lifespan(main.app):
            pass

        assert captured
        with patch("app.main.asyncio.sleep", new=sleep_then_cancel):
            # The RuntimeError is swallowed; only the CancelledError (our stop
            # signal) escapes — proving the janitor doesn't die on a bad cleanup.
            with pytest.raises(asyncio.CancelledError):
                await captured[-1]
