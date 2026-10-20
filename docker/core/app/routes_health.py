"""Liveness, readiness and authenticated subsystem diagnostics."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from inspect import isawaitable
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .lm_gateway import lm_studio_auth_headers


def register_health_routes(
    app: FastAPI,
    resolve_user: Callable[[Request], Awaitable[str | None]],
    voice_url: Callable[[], str],
) -> None:
    # ── Health ──────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> Any:
        """Cached health probe. Sub-ms on cache hit, ~PG+Redis+Qdrant RTT on miss.

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

        import httpx as _hx

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

        # Qdrant
        try:
            from .memory import QDRANT_URL

            async with _hx.AsyncClient(timeout=3.0) as c:
                resp = await c.get(f"{QDRANT_URL}/healthz")
                results["qdrant"] = {
                    "status": "ok" if resp.status_code == 200 else "degraded"
                }
        except Exception:
            results["qdrant"] = {"status": "down"}

        # LM Studio
        try:
            lm_url = os.environ.get("LM_STUDIO_URL", "http://100.107.121.5:1234")
            async with _hx.AsyncClient(timeout=5.0) as c:
                resp = await c.get(
                    f"{lm_url}/v1/models",
                    headers=lm_studio_auth_headers(),
                )
                models = [m["id"] for m in resp.json().get("data", [])]
                results["lm_studio"] = {
                    "status": "ok",
                    "models_loaded": len(models),
                    "models": models,
                }
        except Exception:
            from .llm_router import claude_primary_configured

            # Dominus powered off is normal. With Claude primary configured the
            # local model is only the fallback: report it as standing by, not
            # as an outage that degrades the overall status.
            results["lm_studio"] = (
                {"status": "standby", "role": "fallback"}
                if claude_primary_configured()
                else {"status": "down"}
            )

        results.update(await _image_subsystems())

        # Embedding service
        try:
            from .memory import INFERENCE_URL

            async with _hx.AsyncClient(timeout=5.0) as c:
                resp = await c.get(f"{INFERENCE_URL}/health")
                results["embeddings"] = {
                    "status": "ok" if resp.status_code == 200 else "degraded"
                }
        except Exception:
            results["embeddings"] = {"status": "down"}

        # Voice service. Switched off, it is not probed and not counted.
        from .voice_client import fish_voice_enabled, voice_enabled

        if not voice_enabled():
            results["voice"] = {"status": "disabled"}
        elif fish_voice_enabled():
            # A cloud API: probing it would spend a request on every check.
            results["voice"] = {"status": "ok", "backend": "fish"}
        else:
            try:
                async with _hx.AsyncClient(timeout=5.0) as c:
                    resp = await c.get(f"{voice_url()}/health")
                    results["voice"] = {
                        "status": "ok" if resp.status_code == 200 else "degraded"
                    }
            except Exception:
                results["voice"] = {"status": "down"}

        # Overall ("standby": an idle fallback, e.g. Dominus off behind Claude)
        statuses = [
            v["status"]
            for v in results.values()
            if v["status"] not in {"disabled", "standby"}
        ]
        if all(s == "ok" for s in statuses):
            overall = "ok"
        elif results["database"]["status"] == "down":
            overall = "critical"
        elif any(s == "down" for s in statuses):
            overall = "degraded"
        else:
            overall = "ok"

        return {"status": overall, "subsystems": results}


async def _image_subsystems() -> dict[str, Any]:
    import httpx as _hx

    results: dict[str, Any] = {}
    # Report the selected renderer without waking an unused GPU service.
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
    elif image_provider == "comfyui":
        # The gateway health document is safe without a GPU lease; never
        # query the protected facade from a health probe.
        try:
            lm_url = os.environ.get(
                "LM_STUDIO_URL", "http://100.107.121.5:1234"
            ).rstrip("/")
            async with _hx.AsyncClient(
                timeout=5.0, trust_env=False, follow_redirects=False
            ) as c:
                resp = await c.get(f"{lm_url}/health")
                resp.raise_for_status()
                gateway_health = resp.json()
                comfy_status = gateway_health.get("comfyui_status", "unavailable")
                results["comfyui"] = {
                    "status": "ok" if comfy_status == "ok" else "down",
                    "source": "gpu_gateway",
                }
        except Exception:
            results["comfyui"] = {"status": "down"}
        results["images"] = {"provider": "comfyui", **results["comfyui"]}
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
