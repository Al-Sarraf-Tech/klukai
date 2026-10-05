"""Her Day + wardrobe history routes.

GET /api/her-day          → where she is, what she has on and why, today's roster
GET /api/wardrobe/log    → the wardrobe calendar (what she wore, newest first)

The outfit picker itself (/api/outfits, /api/costume) lives in routes_extras.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request

from . import context
from . import error_codes as ec

logger = logging.getLogger(__name__)


async def _get_user_id(request: Request) -> str | None:
    from .auth import get_user_from_token

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return await get_user_from_token(auth[7:])


def register_wardrobe_routes(app: FastAPI) -> None:
    @app.get("/api/her-day")
    async def api_her_day(request: Request):
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        from . import her_day, wardrobe

        level = (await context.affection.get_state(user_id)).level
        now = await her_day.her_now(
            user_id, level,
            game_active=await context.router.is_game_active(),
            mission=her_day.active_mission(),
        )
        row = await wardrobe.ensure_today(user_id, level)
        weather = row.weather or {}
        return {
            "date": now.day.isoformat(),
            "outfit": wardrobe.outfit_payload(
                now.outfit, reason=now.outfit_reason, source=now.outfit_source, level=level,
            ),
            "status": now.status(level),
            "schedule": now.schedule(level),
            "weather": (
                {"temp_c": weather.get("temp_c"), "condition": weather.get("condition")}
                if weather else None
            ),
        }

    @app.get("/api/wardrobe/log")
    async def api_outfit_history(request: Request, days: int = 30):
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        from . import wardrobe

        try:
            rows = await wardrobe.history(user_id, days)
        except Exception as e:
            logger.warning("Wardrobe history unavailable: %s", e)
            rows = []
        level = (await context.affection.get_state(user_id)).level
        cat = wardrobe.catalog()
        # Days in an outfit she no longer acknowledges at his level are left out.
        return {
            "history": [
                {**r, "name": o.name}
                for r in rows
                if wardrobe.is_visible(o := wardrobe.lookup(r["outfit_id"], cat), level)
            ]
        }
