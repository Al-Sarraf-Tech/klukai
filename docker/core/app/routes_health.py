"""Health routes split out of app/routes.py for file-size hygiene.

The cached/live/ready probes and the deep per-subsystem health aggregation.
``register_health_routes`` is called from ``register_routes`` at the point in
the registration order these endpoints previously occupied.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .lm_gateway import lm_studio_auth_headers
from .routes import _get_user_id
from .voice_client import voice_url as _voice_url


def register_health_routes(app: FastAPI) -> None:
    """Attach the health endpoints to *app*."""

    # ── Health ──────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health():
        """Cached health probe. Sub-ms on cache hit, ~PG+Redis+Qdrant RTT on miss.

        See app/observability/health_cache.py for TTL + refresh semantics.
        SLO: p99 ≤ 30ms (per docs/slos.md).
        """
        from .observability.health_cache import get_cached_health

        return await get_cached_health()

    @app.get("/api/health/live")
    async def health_live():
        """Liveness probe — process-only, no backend pings.

        Returns 200 unless the process itself is broken. K8s-friendly
        liveness convention: liveness failures cause restart, so we
        intentionally don't fail on a backend outage here.
        """
        from .observability.health_cache import get_live_health

        return get_live_health()

    @app.get("/api/health/ready")
    async def health_ready():
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
    async def subsystem_health(request: Request):
        """Deep health check of all external subsystems.

        Returns per-subsystem status so the UI can show degradation clearly
        instead of failures being silent.
        """
        user_id = await _get_user_id(request)
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

            r = aioredis.from_url(REDIS_URL, decode_responses=True)
            await r.ping()
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
            results["lm_studio"] = {"status": "down"}

        # ComfyUI is deliberately not queried through its protected facade:
        # every facade request needs an active lease and health checks must not
        # acquire, refresh, or extend GPU ownership.  The gateway performs a
        # safe internal reachability probe in its public health document.
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

        # Voice service
        try:
            async with _hx.AsyncClient(timeout=5.0) as c:
                resp = await c.get(f"{_voice_url()}/health")
                results["voice"] = {
                    "status": "ok" if resp.status_code == 200 else "degraded"
                }
        except Exception:
            results["voice"] = {"status": "down"}

        # Overall
        statuses = [v["status"] for v in results.values()]
        if all(s == "ok" for s in statuses):
            overall = "ok"
        elif results["database"]["status"] == "down":
            overall = "critical"
        elif any(s == "down" for s in statuses):
            overall = "degraded"
        else:
            overall = "ok"

        return {"status": overall, "subsystems": results}
