"""Companion Core: FastAPI application with WebSocket, memory, and LLM routing."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .context import (
    SESSION_ID,
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


async def generate_daily_recap(affection_level: int) -> str | None:
    """Generate a daily recap by summarizing today's messages via LLM."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        pool = get_pool()
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT role, content FROM companion_messages "
                    "WHERE user_id = 'jalsarraf' AND created_at::date = %s::date "
                    "ORDER BY created_at ASC LIMIT 40",
                    (today,),
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
    """Deliver a proactive message via WebSocket or push notification."""
    if ws.is_connected("default"):
        await ws.send_proactive("default", message)
    else:
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
    proactive.set_session_getter(lambda: memory.get_session(session_id("jalsarraf")))
    proactive.start()
    await events_init()
    load_personality()

    # Warm up primary chat model on startup
    logger.info("Warming up primary LLM model...")
    try:
        await router.keepalive()
        logger.info("LLM warmup complete")
    except Exception as e:
        logger.warning("LLM warmup failed (will retry on first message): %s", e)

    # Start periodic keepalive to prevent model eviction (25-min TTL)
    _keepalive_task = asyncio.create_task(_keepalive_loop())
    logger.info("Klukai companion core started (keepalive every 20 min)")

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

# Register all HTTP routes from routes.py
register_routes(app)

# Register WebSocket endpoint from chat.py
register_websocket(app)

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
