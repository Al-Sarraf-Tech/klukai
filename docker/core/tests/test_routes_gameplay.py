"""Tests for gameplay route handlers: gift, mission, costume, milestones.

Same closure-extraction pattern as test_routes_auth_affection.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
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


def _mk_aff_state(score: int = 500, level: int = 5):
    return SimpleNamespace(
        score=score, level=level, level_name="Trusted",
        consecutive_days=7, total_interactions=100,
        first_interaction=None,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Gift route
# ═══════════════════════════════════════════════════════════════════════════


class TestGiftRoute:
    def test_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/gift", "POST") in paths

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from app.routes import GiftRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/gift", "POST")
        with patch("app.routes._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(GiftRequest(gift="flowers"), _mk_unauth_request())
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_loved_gift_gives_max_bonus(self):
        from app.routes import GiftRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/gift", "POST")

        state = _mk_aff_state(score=400, level=4)
        personality = {
            "gift_preferences": {"loved": ["motorcycle gear"]},
            "gift_reactions": {"loved": "Oh. You remembered."},
        }
        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=False)

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes.affection.get_state", new=AsyncMock(return_value=state)), \
             patch("app.routes.affection._save_state", new=AsyncMock()), \
             patch("app.routes.load_personality", return_value=personality), \
             patch("app.routes.ws", ws_mock):
            result = await handler(
                GiftRequest(gift="motorcycle gear"),
                _mk_request(),
            )

        assert result["tier"] == "loved"
        assert result["bonus"] == 10
        assert "remembered" in result["reaction"]
        assert result["new_score"] == 410

    @pytest.mark.asyncio
    async def test_disliked_gift_decrements_score(self):
        from app.routes import GiftRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/gift", "POST")

        state = _mk_aff_state(score=500, level=5)
        personality = {
            "gift_preferences": {},
            "gift_reactions": {"disliked": "...Noted."},
        }
        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=False)

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes.affection.get_state", new=AsyncMock(return_value=state)), \
             patch("app.routes.affection._save_state", new=AsyncMock()), \
             patch("app.routes.load_personality", return_value=personality), \
             patch("app.routes.ws", ws_mock):
            result = await handler(GiftRequest(gift="garbage"), _mk_request())

        assert result["tier"] == "disliked"
        assert result["bonus"] == -1
        assert result["new_score"] == 499

    @pytest.mark.asyncio
    async def test_score_clamped_to_zero_floor(self):
        from app.routes import GiftRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/gift", "POST")

        state = _mk_aff_state(score=0, level=0)
        personality = {
            "gift_preferences": {},
            "gift_reactions": {"disliked": "..."},
        }
        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=False)

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes.affection.get_state", new=AsyncMock(return_value=state)), \
             patch("app.routes.affection._save_state", new=AsyncMock()), \
             patch("app.routes.load_personality", return_value=personality), \
             patch("app.routes.ws", ws_mock):
            result = await handler(GiftRequest(gift="garbage"), _mk_request())

        assert result["new_score"] == 0  # Clamped


# ═══════════════════════════════════════════════════════════════════════════
# Mission route
# ═══════════════════════════════════════════════════════════════════════════


class TestMissionRoute:
    def test_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/mission", "POST") in paths

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/mission", "POST")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_unauth_request())
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_deployed_status(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/mission", "POST")

        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=False)

        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.affection.get_state",
                   new=AsyncMock(return_value=_mk_aff_state())), \
             patch("app.routes_extras.asyncio.create_task"), \
             patch("app.routes_extras.ws", ws_mock):
            result = await handler(_mk_request())

        assert result == {"status": "deployed"}


# ═══════════════════════════════════════════════════════════════════════════
# Milestones route
# ═══════════════════════════════════════════════════════════════════════════


class TestMilestonesRoute:
    def test_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/milestones", "GET") in paths

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/milestones", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_unauth_request())
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_milestones_list(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/milestones", "GET")

        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.memory.get_milestones",
                   new=AsyncMock(return_value=[{"event": "first_meeting"}])):
            result = await handler(_mk_request())

        assert result == {"milestones": [{"event": "first_meeting"}]}


# ═══════════════════════════════════════════════════════════════════════════
# Costume routes (GET + POST)
# ═══════════════════════════════════════════════════════════════════════════


class TestCostumeGetRoute:
    def test_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/costume", "GET") in paths

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/costume", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_unauth_request())
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_current_costume(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/costume", "GET")

        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")):
            result = await handler(_mk_request())
        assert "costume" in result


class TestCostumeSetRoute:
    def test_registered(self):
        app = _app_with_routes()
        paths = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
        assert ("/api/costume", "POST") in paths

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from app.routes import CostumeRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/costume", "POST")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(CostumeRequest(costume="blazing_star"), _mk_unauth_request())
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_rejects_invalid_costume(self):
        from app.routes import CostumeRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/costume", "POST")

        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")):
            resp = await handler(CostumeRequest(costume="bogus"), _mk_request())
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_accepts_unlocked_costume(self):
        # astral_luminous unlocks at affection level 4; a level-9 Commander can wear it.
        from app.routes import CostumeRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/costume", "POST")

        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.affection.get_state",
                   new=AsyncMock(return_value=SimpleNamespace(level=9))):
            result = await handler(CostumeRequest(costume="astral_luminous"), _mk_request())
        assert result == {"costume": "astral_luminous"}

    @pytest.mark.asyncio
    async def test_rejects_locked_costume(self):
        # A costume gated above the Commander's current affection level is refused.
        from app.routes import CostumeRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/costume", "POST")

        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.affection.get_state",
                   new=AsyncMock(return_value=SimpleNamespace(level=0))):
            resp = await handler(CostumeRequest(costume="astral_luminous"), _mk_request())
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_accepts_level_zero_costume_without_unlock(self):
        # blazing_star is unlock-level 0 — always wearable.
        from app.routes import CostumeRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/costume", "POST")

        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.affection.get_state",
                   new=AsyncMock(return_value=SimpleNamespace(level=0))):
            result = await handler(CostumeRequest(costume="blazing_star"), _mk_request())
        assert result == {"costume": "blazing_star"}
