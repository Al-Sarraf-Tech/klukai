"""HTTP surface for Her Day and the wardrobe calendar."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from app import context
from app import her_day as hd
from app import wardrobe as w
from app.routes_wardrobe import register_wardrobe_routes


def _endpoint(path: str, method: str = "GET"):
    app = FastAPI()
    register_wardrobe_routes(app)
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _request(token: str | None = "good") -> MagicMock:
    req = MagicMock()
    req.headers = {"Authorization": f"Bearer {token}"} if token else {}
    return req


@contextmanager
def _services(level: int = 5, user: str | None = "claude", game: bool = False):
    aff = MagicMock()
    aff.get_state = AsyncMock(return_value=SimpleNamespace(level=level))
    router = MagicMock()
    router.is_game_active = AsyncMock(return_value=game)
    with patch.object(context, "affection", aff), patch.object(context, "router", router), \
         patch.object(context, "proactive", SimpleNamespace(mission_active=False)), \
         patch("app.auth.get_user_from_token", AsyncMock(return_value=user)):
        yield


def _her_now(level_blocks=None):
    blocks = level_blocks or [
        hd.Block("afternoon", 14, 18, "hangar", "Hangar", "tuning the suspension",
                 private=True, outfit="hangar_coveralls"),
    ]
    return hd.HerNow(
        day=date(2026, 10, 4), hour=15, outfit=w.catalog()["hangar_coveralls"],
        outfit_source="roster", outfit_reason="Off-duty plans.", location="Hangar",
        activity="tuning the suspension", block=blocks[0], blocks=blocks,
    )


class TestHerDay:
    async def test_requires_auth(self):
        with _services(user=None):
            resp = await _endpoint("/api/her-day")(_request(None))
        assert resp.status_code == 401

    async def test_full_payload(self):
        row = w.HerDayRow(date(2026, 10, 4), "off_duty_cap", "Off-duty plans.",
                          weather={"temp_c": 14.2, "condition": "clear", "code": 0})
        her_now = AsyncMock(return_value=_her_now())
        with _services(level=5, game=True), \
             patch("app.her_day.her_now", her_now), \
             patch("app.wardrobe.ensure_today", AsyncMock(return_value=row)):
            out = await _endpoint("/api/her-day")(_request())
        assert out["date"] == "2026-10-04"
        assert out["outfit"]["id"] == "hangar_coveralls"
        assert out["outfit"]["source"] == "roster"
        assert out["outfit"]["reason"] == ""
        assert out["status"]["label"] == "Hangar · tuning the suspension"
        assert out["schedule"] == [{
            "slot": "afternoon", "start": "1400", "end": "1800", "location": "Hangar",
            "activity": "tuning the suspension", "current": True,
        }]
        assert out["weather"] == {"temp_c": 14.2, "condition": "clear"}
        assert her_now.await_args.kwargs == {"game_active": True, "mission": None}

    async def test_private_life_hidden_and_no_weather(self):
        row = w.HerDayRow(date(2026, 10, 4), "blazing_star", "A duty day.")
        with _services(level=1), \
             patch("app.her_day.her_now", AsyncMock(return_value=_her_now())), \
             patch("app.wardrobe.ensure_today", AsyncMock(return_value=row)):
            out = await _endpoint("/api/her-day")(_request())
        assert out["status"]["label"] == "Off duty"
        assert out["schedule"][0]["location"] == "Off duty"
        assert out["weather"] is None


class TestWardrobeLog:
    async def test_requires_auth(self):
        with _services(user=None):
            resp = await _endpoint("/api/wardrobe/log")(_request(None))
        assert resp.status_code == 401

    async def test_named_history(self):
        rows = [{"day": "2026-10-03", "outfit_id": "speed_star", "reason": "x", "requested": False}]
        hist = AsyncMock(return_value=rows)
        with _services(), patch("app.wardrobe.history", hist):
            out = await _endpoint("/api/wardrobe/log")(_request(), days=7)
        hist.assert_awaited_once_with("claude", 7)
        assert out == {"history": [{**rows[0], "name": "Speed Star"}]}

    async def test_db_down_is_an_empty_calendar(self):
        with _services(), patch("app.wardrobe.history", AsyncMock(side_effect=RuntimeError("pool"))):
            out = await _endpoint("/api/wardrobe/log")(_request())
        assert out == {"history": []}


class TestCostumeGetIsWhatSheHasOn:
    @pytest.mark.asyncio
    async def test_get_costume_returns_today(self):
        from tests.test_routes_extras_coverage import _app_with_routes, _find_route, _mk_aff_state, _mk_request

        handler = _find_route(_app_with_routes(), "/api/costume", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.affection.get_state", new=AsyncMock(return_value=_mk_aff_state(level=5))), \
             patch("app.her_day.her_now", AsyncMock(return_value=_her_now())):
            assert await handler(_mk_request()) == {"costume": "hangar_coveralls"}

    @pytest.mark.asyncio
    async def test_set_costume_survives_a_db_blip(self):
        from app.routes import CostumeRequest
        from tests.test_routes_extras_coverage import _app_with_routes, _find_route, _mk_aff_state, _mk_request

        handler = _find_route(_app_with_routes(), "/api/costume", "POST")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.affection.get_state", new=AsyncMock(return_value=_mk_aff_state(level=5))), \
             patch("app.routes_extras.memory.store_fact", new=AsyncMock()), \
             patch("app.wardrobe.set_requested", new=AsyncMock(side_effect=RuntimeError("down"))), \
             patch("app.audit.log", new=AsyncMock()):
            assert await handler(CostumeRequest(costume="speed_star"), _mk_request()) == {"costume": "speed_star"}


class TestReviewFixesInRoutes:
    @pytest.mark.asyncio
    async def test_log_hides_outfits_she_no_longer_acknowledges(self):
        rows = [{"day": "2026-10-03", "outfit_id": "indigo_oath", "reason": "", "requested": True},
                {"day": "2026-10-02", "outfit_id": "speed_star", "reason": "x", "requested": False}]
        with _services(level=5), patch("app.wardrobe.history", AsyncMock(return_value=rows)):
            out = await _endpoint("/api/wardrobe/log")(_request())
        assert [r["outfit_id"] for r in out["history"]] == ["speed_star"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("oid,level,decision", [
        ("indigo_oath", 8, "not_today"),
        ("klukadile_pajamas", 9, "deny"),
        ("black_cat_ops", 5, "occasion_only"),
    ])
    async def test_pwa_button_runs_the_decision_table(self, oid, level, decision):
        from app.routes import CostumeRequest
        from tests.test_routes_extras_coverage import _app_with_routes, _find_route, _mk_aff_state, _mk_request

        handler = _find_route(_app_with_routes(), "/api/costume", "POST")
        store = AsyncMock()
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.affection.get_state", new=AsyncMock(return_value=_mk_aff_state(level=level))), \
             patch("app.routes_extras.memory.store_fact", store), \
             patch("app.her_day.her_now", AsyncMock(return_value=_her_now())), \
             patch("app.wardrobe.occasions_for", AsyncMock(return_value=frozenset())):
            resp = await handler(CostumeRequest(costume=oid), _mk_request())
        assert resp.status_code == 403
        assert decision.encode() in resp.body
        store.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_outfits_list_marks_the_onesie_unselectable(self):
        from tests.test_routes_extras_coverage import _app_with_routes, _find_route, _mk_aff_state, _mk_request

        handler = _find_route(_app_with_routes(), "/api/outfits", "GET")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes_extras.affection.get_state", new=AsyncMock(return_value=_mk_aff_state(level=9))), \
             patch("app.her_day.her_now", AsyncMock(return_value=_her_now())):
            out = await handler(_mk_request())
        by_id = {o["id"]: o for o in out["outfits"]}
        assert by_id["klukadile_pajamas"]["unlocked"] is False
        assert by_id["indigo_oath"]["unlocked"] is True
