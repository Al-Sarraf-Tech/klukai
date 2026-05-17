"""Companion Core: FastAPI application with WebSocket, memory, and LLM routing."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

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
from .models import SessionState
from .personality import load_personality
from .push import send_push
from .routes import register_routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


# ── Lifecycle ────────────────────────────────────────────────────────────────


async def run_migration() -> None:
    """Run all companion SQL migrations on startup."""
    migration_dir = Path(__file__).parent.parent / "migrations"
    migration_files = sorted(migration_dir.glob("*.sql"))

    pool = get_pool()
    for migration_path in migration_files:
        sql = migration_path.read_text()
        try:
            async with pool.connection() as conn:
                await conn.execute(sql)
                await conn.commit()
            logger.info("Migration %s applied", migration_path.name)
        except Exception as e:
            logger.warning("Migration %s may already be applied: %s", migration_path.name, e)


async def generate_daily_recap(affection_level: int, user_id: str = "jalsarraf") -> str | None:
    """Generate a daily recap by summarizing today's messages via LLM."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        pool = get_pool()
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT role, content FROM companion_messages "
                    "WHERE user_id = %s AND created_at::date = %s::date "
                    "ORDER BY created_at ASC LIMIT 40",
                    (user_id, today,),
                )
            ).fetchall()

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

        config = await router.route("recap", SessionState(conversation_id="recap"))
        full = []
        async for token in router.stream(prompt, [{"role": "user", "content": prompt}], config):
            full.append(token)
        return "".join(full) if full else None
    except Exception as e:
        logger.warning("Daily recap generation failed: %s", e)
        return None


async def proactive_callback(message: str) -> None:
    """Deliver a proactive message to ALL connected users via WebSocket or push."""
    delivered = False
    for user_id in list(ws._connections.keys()):
        if ws.is_connected(user_id):
            await ws.send_proactive(user_id, message)
            delivered = True
    if not delivered:
        await send_push(title="Klukai", body=message)


async def _keepalive_loop() -> None:
    """Periodically ping LM Studio models to keep them loaded in VRAM."""
    from .llm_router import _KEEPALIVE_INTERVAL
    while True:
        await asyncio.sleep(_KEEPALIVE_INTERVAL)
        try:
            await router.keepalive()
        except Exception as e:
            logger.warning("Keepalive loop error: %s", e)

_keepalive_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown."""
    global _keepalive_task
    await init_pool(min_size=2, max_size=10)
    await run_migration()
    from .auth import init_users
    await init_users()
    await memory.init()
    await router.init()
    await mcp.init()
    await affection.init()
    proactive.set_callback(proactive_callback)
    proactive.set_recap_callback(generate_daily_recap)
    # Session getter tries the primary user first, then any connected user
    async def _get_any_session(user_id: str = None):
        # Try specific user first if provided
        if user_id:
            s = await memory.get_session(session_id(user_id))
            if s:
                return s
        # Try any connected user
        for uid in list(ws._connections.keys()):
            s = await memory.get_session(session_id(uid))
            if s:
                return s
        # Fallback to primary user
        return await memory.get_session(session_id("jalsarraf"))
    proactive.set_session_getter(_get_any_session)
    proactive.start()
    await events_init()
    load_personality()

    # Load-on-demand: no startup warmup, no periodic keepalive.
    # LM Studio's JIT TTL evicts dolphin after idle; first message
    # reloads it (~3-5s cold-start). Set KLUKAI_LLM_KEEPALIVE=1 to
    # re-enable the keepalive loop.
    if os.environ.get("KLUKAI_LLM_KEEPALIVE") == "1":
        try:
            await router.keepalive()
        except Exception as e:
            logger.warning("LLM warmup failed: %s", e)
        _keepalive_task = asyncio.create_task(_keepalive_loop())
        logger.info("LLM keepalive enabled (KLUKAI_LLM_KEEPALIVE=1)")
    else:
        logger.info("LLM load-on-demand mode (no keepalive, JIT TTL handles unload)")

    # Session token cleanup — runs every 6 hours
    async def _session_cleanup_loop():
        while True:
            await asyncio.sleep(3600)
            try:
                from .auth import cleanup_expired_sessions
                await cleanup_expired_sessions()
            except Exception:
                pass
    asyncio.create_task(_session_cleanup_loop())

    logger.info("Klukai companion core started (session cleanup every 1h)")

    yield

    if _keepalive_task:
        _keepalive_task.cancel()
    proactive.stop()
    await events_close()
    await memory.close()
    await router.close()
    await mcp.close()
    await affection.close()
    await close_pool()
    logger.info("Klukai companion core stopped")


app = FastAPI(title="Companion Core", version="0.1.0", lifespan=lifespan)


# ── Security middleware ─────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://klukai.appnest.cc", "http://localhost:8300"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: StarletteResponse = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


app.add_middleware(_SecurityHeadersMiddleware)


# ── Request ID tracing ──────────────────────────────────────────────────────

class _RequestIdMiddleware(BaseHTTPMiddleware):
    """Inject X-Request-ID on every request. Propagate if client sent one."""

    async def dispatch(self, request: Request, call_next):
        import uuid
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        request.state.request_id = rid
        response: StarletteResponse = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response


app.add_middleware(_RequestIdMiddleware)


# ── Rate limiting ───────────────────────────────────────────────────────────

# Map path prefix -> rate limit bucket. Longest match wins.
_RATE_LIMIT_BUCKETS: dict[str, str] = {
    "/api/auth/login":         "login",
    "/api/user/export":        "export",
    "/api/user/stats":         "stats",
    "/api/tts":                "tts",
    "/api/stt":                "stt",
    "/api/generate-image":     "image_gen",
    "/api/gift":               "gift",
    "/api/mission":            "mission",
    "/api/memories/search":    "search",
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

    async def dispatch(self, request: Request, call_next):
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
            identity = request.client.host if request.client else "anon"

        from .rate_limit import check_and_consume, RateLimitExceeded
        try:
            remaining, retry_after = await check_and_consume(identity, bucket)
        except RateLimitExceeded as exc:
            return JSONResponse(
                {"error": "Too many requests", "bucket": exc.bucket,
                 "retry_after": exc.retry_after},
                status_code=429,
                headers={"Retry-After": str(exc.retry_after)},
            )

        response: StarletteResponse = await call_next(request)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Bucket"] = bucket
        return response


app.add_middleware(_RateLimitMiddleware)


# ── Metrics recording ───────────────────────────────────────────────────────

class _MetricsMiddleware(BaseHTTPMiddleware):
    """Count requests and record latency for every handled request."""

    async def dispatch(self, request: Request, call_next):
        import time
        from . import metrics
        start = time.monotonic()
        try:
            response: StarletteResponse = await call_next(request)
        except Exception:
            elapsed_ms = (time.monotonic() - start) * 1000
            metrics.incr("requests_total", path=request.url.path, status="500")
            metrics.observe_latency("request_latency_ms", elapsed_ms, path=request.url.path)
            raise
        elapsed_ms = (time.monotonic() - start) * 1000
        metrics.incr("requests_total", path=request.url.path, status=str(response.status_code))
        metrics.observe_latency("request_latency_ms", elapsed_ms, path=request.url.path)
        return response


app.add_middleware(_MetricsMiddleware)


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception on %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse({"error": "Internal server error"}, status_code=500)


# Register all HTTP routes from routes.py
register_routes(app)

# Register WebSocket endpoint from chat.py
register_websocket(app)

# ── OpenTelemetry instrumentation (Phase 3.2) ─────────────────────────────
# Fail-soft: if OTEL_EXPORTER_OTLP_ENDPOINT is unset (dev / standalone),
# all OTel paths become no-ops. If init fails, log warning and continue —
# klukai never goes down because of observability misconfiguration.
try:
    from .observability.tracing import init_tracing, instrument_fastapi, instrument_httpx
    if init_tracing():
        instrument_fastapi(app)
        instrument_httpx()
except Exception as _otel_err:
    logger.warning("OTel setup skipped: %s", _otel_err)

# Serve Flutter PWA static files (mounted last so API routes take priority)
static_dir = Path("/app/static")
if static_dir.exists():
    app.mount("/app", StaticFiles(directory=str(static_dir), html=True), name="pwa")


# ── Re-exports for backward compatibility ────────────────────────────────────
# Tests and other modules that ``from app.main import ...`` will still work.

_fix_narration = _fix_narration
_enhance_image_prompt = None  # Lazy re-export below


def __getattr__(name: str):
    """Lazy re-exports for backward compatibility."""
    if name == "_enhance_image_prompt":
        from .helpers import enhance_image_prompt
        return enhance_image_prompt
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
