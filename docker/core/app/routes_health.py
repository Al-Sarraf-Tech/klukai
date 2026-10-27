"""Liveness, readiness and authenticated subsystem diagnostics."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register_health_routes(
    app: FastAPI,
    resolve_user: Callable[[Request], Awaitable[str | None]],
) -> None:
    # ── Health ──────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> Any:
        """Cached health probe. Sub-ms on cache hit, ~PG+Redis RTT on miss.

        See app/observability/health_cache.py for TTL + refresh semantics.
        SLO: p99 ≤ 30ms (per docs/slos.md).
        """
        from .observability.health_cache import get_cached_health

        return await get_cached_health()

    @app.get("/api/health/live")
    async def health_live() -> Any:
        """Liveness probe — process-only, no backend pings.

        Returns 200 unless the process itself is broken. K8s-friendly
        liveness convention: liveness failures cause restart, so we
        intentionally don't fail on a backend outage here.
        """
        from .observability.health_cache import get_live_health

        return get_live_health()

    @app.get("/api/health/ready")
    async def health_ready() -> Any:
        """Readiness probe — full uncached deep check.

        Forces cache refresh; reports unhealthy if any required backend
        is down. K8s-friendly readiness convention: readiness failures
        remove the pod from service rotation without restarting it.
        """
        from .observability.health_cache import get_fresh_health

        result = await get_fresh_health()
        # 503 if backends are down so load balancers / Cloudflare can
        # short-circuit traffic instead of forwarding to a broken core.
        if result.get("status") == "unhealthy":
            from fastapi.responses import JSONResponse

            return JSONResponse(content=result, status_code=503)
        return result

    # ── Subsystem health (S+ feature: loud failure detection) ───────────

    @app.get("/api/health/subsystems")
    async def subsystem_health(request: Request) -> Any:
        """Deep health check of all external subsystems.

        Returns per-subsystem status so the UI can show degradation clearly
        instead of failures being silent.
        """
        user_id = await resolve_user(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)

        results = {}

        # Database
        try:
            from .db import check_health as db_health

            db = await db_health()
            results["database"] = {
                "status": "ok" if db.get("status") == "ok" else "down",
                **db,
            }
        except Exception:
            results["database"] = {"status": "down"}

        # Redis
        try:
            from .memory import REDIS_URL
            import redis.asyncio as aioredis

            _redis_factory: Callable[..., aioredis.Redis] = aioredis.from_url
            r = _redis_factory(REDIS_URL, decode_responses=True)
            ping_result = r.ping()
            if isawaitable(ping_result):
                await ping_result
            await r.aclose()
            results["redis"] = {"status": "ok"}
        except Exception:
            results["redis"] = {"status": "down"}

        # Language models: Claude only. A paid API, so only its configuration
        # is checked, never a request per probe.
        from .llm_router import claude_primary_configured

        results["llm"] = {
            "status": "ok" if claude_primary_configured() else "down",
            "provider": "anthropic",
        }

        results.update(await _image_subsystems())

        # Voice (Fish Audio speech in and out). Switched off, it is not
        # counted; a cloud API, so only its configuration is checked.
        from .voice_client import fish_configured, voice_enabled

        if not voice_enabled():
            results["voice"] = {"status": "disabled"}
        else:
            results["voice"] = {
                "status": "ok" if fish_configured() else "down",
                "backend": "fish",
            }

        # Overall
        statuses = [v["status"] for v in results.values() if v["status"] != "disabled"]
        if all(s == "ok" for s in statuses):
            overall = "ok"
        elif results["database"]["status"] == "down":
            overall = "critical"
        else:
            # Anything not ok ("down", or "degraded" answers) degrades it.
            overall = "degraded"

        return {"status": overall, "subsystems": results}


async def _image_subsystems() -> dict[str, Any]:
    results: dict[str, Any] = {}
    from .image_provider import cloud_images_ready, selected_provider

    image_provider = selected_provider()
    if image_provider == "agy":
        from .agy_image_provider import agy_images_ready

        ready = await agy_images_ready()
        results["images"] = {
            "provider": "agy",
            "status": "ok" if ready else "down",
            "source": "host_worker",
            "google_auth": "not_checked",
        }
    elif image_provider == "openai":
        ready = await asyncio.to_thread(cloud_images_ready)
        results["images"] = {
            "provider": "openai",
            "status": "ok" if ready else "down",
            "source": "configuration",
            "provider_auth": "not_checked",
        }
    else:
        results["images"] = {"provider": image_provider, "status": "disabled"}

    return results
