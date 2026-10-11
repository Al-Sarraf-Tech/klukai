"""Internal delivery hooks — split from app/routes_extras3.py (file-size hygiene).

The /internal/* endpoints are called by the events bridge, never by a user:
there is no session on this path, so each one is gated by a shared secret
(``_internal_token_ok``) and must never be reachable from the public surface.
"""

from __future__ import annotations

import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def _internal_token_ok(request: Request) -> bool:
    """Gate for the /internal/* hooks: X-Internal-Token vs CORE_INTERNAL_TOKEN.

    Fails closed — an unset token means nobody can call them, not everybody.
    Compares bytes, not str: compare_digest raises TypeError on non-ASCII
    text, and Starlette hands a header byte ≥0x80 through as latin-1 text,
    which must be a 403 rather than a 500.
    """
    import os

    expected = os.environ.get("CORE_INTERNAL_TOKEN", "")
    if not expected:
        return False
    presented = request.headers.get("X-Internal-Token", "")
    return secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def register_internal(app: FastAPI) -> None:
    """Register the /internal/* hooks (shared-secret gated, no user session)."""
    # ── Deferred tasks: internal delivery hook ─────────────────────────────

    @app.post("/internal/deferred/fire")
    async def api_deferred_fire(request: Request):
        """Deliver a deferred task whose timer expired.

        Called by the events bridge, which owns the AMQP side of the delay rail.
        Guarded by a shared secret rather than a user session: there is no user
        on this path, and the endpoint can start work, so it must never be
        reachable from the public surface. Fails closed — an unset token means
        nobody can call it, rather than everybody.
        """
        if not _internal_token_ok(request):
            return JSONResponse({"error": "Forbidden"}, status_code=403)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        task_id = str((body or {}).get("task_id") or "")
        if not task_id:
            return JSONResponse({"error": "task_id required"}, status_code=400)

        from . import deferred
        fired = await deferred.fire(task_id)
        # 200 either way: "already delivered" is a success from the bridge's
        # point of view, and must not make it redeliver forever.
        return {"task_id": task_id, "fired": fired}

    @app.post("/internal/jobs/her-pov/run")
    async def api_her_pov_run(request: Request):
        """Run a durable Her POV job to completion.

        Called by the events-bridge while it holds the unacked queue message
        (prefetch=1 = GPU lease). Same shared-secret gate as deferred/fire.
        Blocks until the pipeline is terminal so the bridge can ack safely.
        """
        if not _internal_token_ok(request):
            return JSONResponse({"error": "Forbidden"}, status_code=403)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        job_id = str((body or {}).get("job_id") or "")
        if not job_id:
            return JSONResponse({"error": "job_id required"}, status_code=400)

        from . import memory_her_pov
        ok = await memory_her_pov.run_job_from_queue(job_id)
        return {"job_id": job_id, "ok": ok}
