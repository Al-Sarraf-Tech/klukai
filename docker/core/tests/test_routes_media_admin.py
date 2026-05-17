"""Tests for media + admin route handlers: TTS, STT, image gen, password change."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _mk_request() -> MagicMock:
    req = MagicMock()
    req.headers = {"Authorization": "Bearer good"}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    req.state = MagicMock()
    req.state.request_id = "test-req-id"
    return req


def _mk_unauth_request() -> MagicMock:
    req = MagicMock()
    req.headers = {}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    req.state = MagicMock()
    return req


def _app_with_routes() -> FastAPI:
    from app.routes import register_routes
    app = FastAPI()
    register_routes(app)
    return app


def _find_route(app: FastAPI, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


# ═══════════════════════════════════════════════════════════════════════════
# TTS
# ═══════════════════════════════════════════════════════════════════════════


class TestTTSRoute:
    def test_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/tts", "POST") in paths

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from app.routes import TTSRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/tts", "POST")
        with patch("app.routes._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(TTSRequest(text="hi", language="en"), _mk_unauth_request())
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_rejects_empty_after_strip(self):
        """Text with only narration actions gets stripped to empty."""
        from app.routes import TTSRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/tts", "POST")

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes._strip_actions_for_tts", return_value="   "):
            resp = await handler(TTSRequest(text="(I sigh)", language="en"), _mk_request())
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# STT
# ═══════════════════════════════════════════════════════════════════════════


class TestSTTRoute:
    def test_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/stt", "POST") in paths

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from app.routes import STTRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/stt", "POST")
        with patch("app.routes._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(STTRequest(audio="base64data"), _mk_unauth_request())
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# Image generation
# ═══════════════════════════════════════════════════════════════════════════


class TestImageGenRoute:
    def test_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/generate-image", "POST") in paths

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from app.routes import ImageGenRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/generate-image", "POST")
        with patch("app.routes._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(
                ImageGenRequest(prompt="klukai in winter outfit"),
                _mk_unauth_request(),
            )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_500_when_gen_fails(self):
        from app.routes import ImageGenRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/generate-image", "POST")

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes.generate_image", new=AsyncMock(return_value=None)):
            resp = await handler(
                ImageGenRequest(prompt="test"),
                _mk_request(),
            )
        assert resp.status_code == 500


# ═══════════════════════════════════════════════════════════════════════════
# Backfill annotations
# ═══════════════════════════════════════════════════════════════════════════


class TestBackfillRoute:
    def test_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/memories/backfill-annotations", "POST") in paths

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/backfill-annotations", "POST")
        with patch("app.routes._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_unauth_request())
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_started_status(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/memories/backfill-annotations", "POST")

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes.asyncio.create_task"):
            result = await handler(_mk_request())
        assert result["status"] == "started"


# ═══════════════════════════════════════════════════════════════════════════
# Memory image + thumbnail GETs
# ═══════════════════════════════════════════════════════════════════════════


class TestMemoryImageRoute:
    def test_image_route_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/memories/{memory_id}/image", "GET") in paths

    def test_thumbnail_route_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/memories/{memory_id}/thumbnail", "GET") in paths


# ═══════════════════════════════════════════════════════════════════════════
# Static / SW / Root
# ═══════════════════════════════════════════════════════════════════════════


class TestStaticRoutes:
    def test_service_worker_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/flutter_service_worker.js", "GET") in paths

    def test_root_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/", "GET") in paths
