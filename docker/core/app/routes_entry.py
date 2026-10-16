"""Root landing resource and retirement of the old root-scoped service worker."""

from __future__ import annotations
from typing import Any
from fastapi import FastAPI


def register_entry_routes(app: FastAPI) -> None:
    @app.get("/flutter_service_worker.js")
    async def root_service_worker() -> Any:
        """Serve self-destructing SW at root scope to kill the old cached SW.

        The Flutter app used to be served at / with a SW at / scope. Now it's
        at /app/ with a SW at /app/ scope. Browsers that still have the old SW
        cached will check /flutter_service_worker.js for updates. This serves
        a version that immediately unregisters itself and clears all caches.
        """
        from fastapi.responses import Response

        sw_code = (
            "self.addEventListener('install', () => self.skipWaiting());\n"
            "self.addEventListener('activate', (e) => {\n"
            "  e.waitUntil(caches.keys()"
            ".then(ns => Promise.all(ns.map(n => caches.delete(n))))"
            ".then(() => self.registration.unregister())"
            ".then(() => self.clients.matchAll({type:'window'}))"
            ".then(cs => cs.forEach(c => c.navigate(c.url))));\n"
            "});\n"
        )
        return Response(
            content=sw_code,
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/")
    async def root() -> Any:
        """Serve login page or redirect to app."""
        from pathlib import Path
        from fastapi.responses import FileResponse

        login_path = Path("/app/static/login.html")
        if login_path.exists():
            return FileResponse(login_path, media_type="text/html")
        return {"status": "companion-core running", "auth": "login page not deployed"}
