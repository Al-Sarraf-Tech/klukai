"""Companion Core: FastAPI application with WebSocket, memory, and LLM routing."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any
from starlette.middleware.base import RequestResponseEndpoint
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

from . import __version__
from .context import (
    affection,
    mcp,
    memory,
    proactive,
    router,
    session_id,
    ws,
)
from .chat import register_websocket
from .db import init_pool, close_pool, get_pool
from .events import init as events_init, close as events_close
from .helpers import fix_narration as _fix_narration
from .migrations import apply_migrations
from .models import SessionState
from .personality import load_personality
from .push import send_push
from .routes import register_routes
from .observability.private_metrics import register_metrics
from .observability.http_middleware import (
    _RequestIdMiddleware as _RequestIdMiddleware,
    _MetricsMiddleware as _MetricsMiddleware,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


from .observability.redaction import (  # noqa: E402
    CredentialLogFilter as _RedactTokenLogFilter,  # noqa: F401 - compatibility
    install_log_redaction,
)

install_log_redaction()


# ── Lifecycle ────────────────────────────────────────────────────────────────


async def run_migration() -> None:
    """Verify and apply companion migrations, failing startup on any error."""
    await apply_migrations(get_pool(), Path(__file__).parent.parent / "migrations")


async def generate_daily_recap(
    affection_level: int, user_id: str = "jalsarraf"
) -> str | None:
    """Generate a daily recap by summarizing today's messages via LLM."""
    try:
        from .proactive.state import LOCAL_TZ, now_local

        # "Today" is the Commander's day. The container runs UTC and the DB
        # session zone is UTC too, so `created_at::date` compared with the
        # container's date meant the 21:00 Chicago run only counted messages
        # after 19:00 local — rarely four, so the recap silently never fired.
        today = now_local().date()
        pool = get_pool()
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    # Skip her own check-ins — recapping them back at her turns
                    # the daily recap into an echo chamber.
                    "SELECT role, content FROM companion_messages "
                    "WHERE user_id = %s "
                    "AND (created_at AT TIME ZONE %s)::date = %s::date "
                    "AND COALESCE(model, '') <> 'proactive' "
                    # Newest first so a busy day keeps its evening; the rows are
                    # put back in order below.
                    "ORDER BY created_at DESC LIMIT 40",
                    (user_id, str(LOCAL_TZ), today),
                )
            ).fetchall()
        rows = rows[::-1]

        if len(rows) < 4:
            return None  # Not enough conversation to recap

        conversation = "\n".join(
            f"{'Commander' if r[0] == 'user' else 'Klukai'}: {r[1][:150]}"
            for r in rows[-20:]
        )

        if affection_level >= 3:
            tone = "Write warmly. You care about this Commander deeply."
        elif affection_level >= 1:
            tone = "Write professionally but with subtle investment."
        else:
            tone = "Write coldly and efficiently."

        prompt = (
            f"You are Klukai writing a brief evening operational log about today's "
            f"interactions with the Commander. {tone} Summarize in 2-3 sentences. "
            f"Use first person. Reference specific topics discussed.\n\n{conversation}"
        )

        from . import background_llm

        recap = await background_llm.text("daily_recap", prompt, max_tokens=2048)
        return recap or None
    except Exception as e:
        logger.warning("Daily recap generation failed: %s", e)
        return None


async def proactive_callback(message: str, user_id: str | None = None) -> bool:
    """Deliver a proactive line: into history always, then live or by push.

    Scheduled proactivity defaults to the primary owner; mission callbacks
    explicitly carry their owner. ``send_proactive`` persists the row even
    with no device connected — she spoke, it lands in history either way —
    then a connected device has it live, otherwise it goes out as a push.

    Returns True iff someone actually received it. The engine's ``_deliver``
    treats False as not delivered, so a promise follow-up that reached no one
    (offline, no push subscriber) is not marked as sent.
    """
    from .decisions import PRIMARY_USER

    recipient = user_id or PRIMARY_USER
    await ws.send_proactive(recipient, message)
    if ws.is_connected(recipient):
        return True
    return (await send_push(title="Klukai", body=message, user_id=recipient)) > 0


async def _session_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            from .auth import cleanup_expired_sessions

            await cleanup_expired_sessions()
        except Exception:
            logger.warning("Session cleanup failed; will retry", exc_info=True)
        # Expired TTL facts (the aichat data service used to purge these
        # hourly); permanent facts are never touched.
        try:
            from . import fact_store

            purged = await fact_store.purge_expired()
            if purged:
                logger.info("Purged %d expired fact rows", purged)
        except Exception:
            logger.warning("Expired fact purge failed; will retry", exc_info=True)


async def _cancel_task(task: asyncio.Task[Any]) -> None:
    if not task.done() and not task.cancelling():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own resources before initialization so partial failures also unwind."""
    from . import job_handoff
    from .auth import init_users
    from .caches import close as _caches_close
    from .rate_limit import close as _rate_limit_close
    from .observability.health_cache import running as _health_checks_running
    from .runtime_tasks import running as _application_work
    from . import weather_client

    async with _application_work(ws) as (cleanup, owners):
        # Register before each init: clients/pool workers can exist even when
        # that initializer raises. ExitStack continues after a cleanup error.
        cleanup.push_async_callback(close_pool)
        cleanup.push_async_callback(_caches_close)
        cleanup.push_async_callback(_rate_limit_close)
        cleanup.push_async_callback(weather_client.close)
        await cleanup.enter_async_context(_health_checks_running())
        await init_pool(min_size=2, max_size=10)
        await run_migration()
        await init_users()
        for resource in (memory, router, mcp, affection):
            cleanup.push_async_callback(resource.close)
            await resource.init()
        owners.push_async_callback(app.state.voice_jobs.close)
        proactive.set_callback(proactive_callback)
        proactive.set_recap_callback(generate_daily_recap)

        # Missing sessions never borrow another user's private context.
        async def _get_user_session(user_id: str | None = None) -> SessionState | None:
            from .decisions import PRIMARY_USER

            return await memory.get_session(session_id(user_id or PRIMARY_USER))

        proactive.set_session_getter(_get_user_session)
        proactive.set_game_active_probe(router.is_game_active)
        cleanup.push_async_callback(events_close)
        await events_init()
        owners.push_async_callback(proactive.close)
        proactive.start()
        load_personality()

        from . import extraction_llm

        logger.info("%s", extraction_llm.startup_line())
        task = asyncio.create_task(_session_cleanup_loop(), name="session-cleanup")
        owners.push_async_callback(_cancel_task, task)
        await owners.enter_async_context(job_handoff.running())
        logger.info("Klukai companion core started (session cleanup every 1h)")
        yield
    logger.info("Klukai companion core stopped")


# docs_url/redoc_url/openapi_url disabled: this is a private single-owner app
# behind Cloudflare; the interactive docs + schema would hand an attacker the
# full endpoint map (incl. admin routes) for free. No auth gate needed when off.
app = FastAPI(
    title="Companion Core",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# ── Security middleware ─────────────────────────────────────────────────────

# Allowed CORS origins are env-driven so dev origins (e.g. http://localhost:8300)
# are never shipped to production. Default is the production web origin only;
# set KLUKAI_CORS_ORIGINS (comma-separated) to add others for local dev.
_CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("KLUKAI_CORS_ORIGINS", "https://klukai.appnest.cc").split(
        ","
    )
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


# The PWA's Chat tab shows her 3D stage in an iframe of this one page, so it
# alone may be framed, and only by Klukai's own origin. Everything else stays
# unframeable.
_SAME_ORIGIN_FRAME_PATHS = frozenset({"/app/companion.html"})


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> StarletteResponse:
        response: StarletteResponse = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path in _SAME_ORIGIN_FRAME_PATHS:
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
        else:
            response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


app.add_middleware(_SecurityHeadersMiddleware)


# ── Request ID tracing ──────────────────────────────────────────────────────


app.add_middleware(_RequestIdMiddleware)


# ── Rate limiting ───────────────────────────────────────────────────────────

# Map path prefix -> rate limit bucket. Longest match wins.
_RATE_LIMIT_BUCKETS: dict[str, str] = {
    "/api/auth/login": "login",
    # change-password shares the login bucket — both are password-guessing
    # surfaces and its docstring promises brute-force protection.
    "/api/user/change-password": "login",
    "/api/user/export": "export",
    "/api/user/stats": "stats",
    "/api/tts": "tts",
    "/api/tts/jobs": "voice_jobs",
    "/api/stt": "stt",
    "/api/generate-image": "image_gen",
    "/api/gift": "gift",
    "/api/mission": "mission",
    "/api/memories/search": "search",
    "/api/portrait/refresh": "portrait_refresh",
}


def _bucket_for_path(path: str) -> str | None:
    """Return the rate limit bucket for a given path, or None to skip."""
    hit = None
    for prefix, bucket in _RATE_LIMIT_BUCKETS.items():
        if path == prefix or path.startswith(prefix + "/"):
            if hit is None or len(prefix) > len(hit[0]):
                hit = (prefix, bucket)
    return hit[1] if hit else None


class _RateLimitMiddleware(BaseHTTPMiddleware):
    """Enforce Redis-backed rate limits on rate-protected endpoints.

    Uses user_id from Authorization header when present; falls back to
    client IP for unauthenticated endpoints like /api/auth/login.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> StarletteResponse:
        path = request.url.path
        bucket = _bucket_for_path(path)
        if bucket is None:
            return await call_next(request)

        # Resolve identity: user_id from bearer token or IP for pre-auth calls
        identity = None
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            try:
                from .auth import get_user_from_token

                identity = await get_user_from_token(auth[7:])
            except Exception:
                identity = None
        if not identity:
            from .helpers import client_ip

            identity = client_ip(request)

        from .rate_limit import consume, refund, RateLimitExceeded

        try:
            remaining, retry_after, receipt = await consume(identity, bucket)
        except RateLimitExceeded as exc:
            return JSONResponse(
                {
                    "error": "Too many requests",
                    "bucket": exc.bucket,
                    "retry_after": exc.retry_after,
                },
                status_code=429,
                headers={"Retry-After": str(exc.retry_after)},
            )

        response: StarletteResponse = await call_next(request)
        if response.status_code == 409 and await refund(receipt):
            # The handler turned the request away (GPU busy, already in
            # progress) without doing the work the limit guards. Give the
            # token back, or one 409 on /api/portrait/refresh locks him out
            # for the whole 10-minute window with nothing rendered.
            remaining += 1
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Bucket"] = bucket
        return response


app.add_middleware(_RateLimitMiddleware)


# ── Metrics recording ───────────────────────────────────────────────────────


app.add_middleware(_MetricsMiddleware)


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled exception on %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse({"error": "Internal server error"}, status_code=500)


# Register all HTTP routes from routes.py
register_routes(app)
register_metrics(app)

# Register WebSocket endpoint from chat.py
register_websocket(app)

# ── OpenTelemetry instrumentation (Phase 3.2) ─────────────────────────────
# Fail-soft: if OTEL_EXPORTER_OTLP_ENDPOINT is unset (dev / standalone),
# all OTel paths become no-ops. If init fails, log warning and continue —
# klukai never goes down because of observability misconfiguration.
try:
    from .observability.tracing import (
        init_tracing,
        instrument_fastapi,
        instrument_httpx,
    )

    if init_tracing():
        instrument_fastapi(app)
        instrument_httpx()
except Exception as _otel_err:
    logger.warning("OTel setup skipped: %s", _otel_err)

# Serve Flutter PWA static files (mounted last so API routes take priority)
static_dir = Path("/app/static")
if static_dir.exists():
    from .static_cache import CachedStaticFiles

    app.mount(
        "/app", CachedStaticFiles(directory=str(static_dir), html=True), name="pwa"
    )


# ── Re-exports for backward compatibility ────────────────────────────────────
# Tests and other modules that ``from app.main import ...`` will still work.

_fix_narration = _fix_narration
_enhance_image_prompt = None  # Lazy re-export below


def __getattr__(name: str) -> Any:
    """Lazy re-exports for backward compatibility."""
    if name == "_enhance_image_prompt":
        from .helpers import enhance_image_prompt

        return enhance_image_prompt
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
