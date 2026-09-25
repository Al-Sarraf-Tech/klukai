"""Gaming awareness: Klukai knows when the Commander is mid-game (ADR-0018).

The router's game-active probe feeds a per-message prompt block and holds
proactive random events off while a game owns the GPU host.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.llm_router import LOCAL_CASUAL, LOCAL_CASUAL_GAMING, LLMRouter
from app.models import SessionState
from app.personality import build_gaming_block, load_personality
from app.proactive import ProactiveEngine


def _router_with_health(*responses) -> LLMRouter:
    r = LLMRouter()
    r._lmstudio_available = True
    r._http = MagicMock()
    r._http.get = AsyncMock(side_effect=list(responses))
    return r


def _health(status: int = 200, game_active: bool = True) -> MagicMock:
    resp = MagicMock(status_code=status)
    resp.json.return_value = {"game_active": game_active}
    return resp


class TestRouterGameActive:
    async def test_uninitialised_router_keeps_last_known_instead_of_crashing(self):
        assert await LLMRouter().is_game_active() is False

    async def test_known_down_gateway_is_not_probed(self):
        r = _router_with_health(_health(game_active=True))
        r._lmstudio_available = False
        assert await r.is_game_active() is False
        r._http.get.assert_not_awaited()

    async def test_game_active_routes_chat_to_gaming_persona(self):
        r = _router_with_health(_health(game_active=True))
        cfg = await r.route("hey", SessionState(conversation_id="c"))
        assert cfg.model == LOCAL_CASUAL_GAMING

    async def test_no_game_routes_to_venice(self):
        r = _router_with_health(_health(game_active=False))
        cfg = await r.route("hey", SessionState(conversation_id="c"))
        assert cfg.model == LOCAL_CASUAL

    async def test_result_is_cached_between_checks(self):
        r = _router_with_health(_health(game_active=True))
        assert await r.is_game_active() is True
        assert await r.is_game_active() is True
        r._http.get.assert_awaited_once()

    @pytest.mark.parametrize("failure", [httpx.ConnectError("down"), _health(status=503)])
    async def test_failed_check_keeps_last_known(self, failure):
        r = _router_with_health(_health(game_active=True), failure)
        assert await r.is_game_active() is True
        r._game_active_last_check = 0.0  # force a re-check
        assert await r.is_game_active() is True


class TestGamingBlock:
    @pytest.fixture(scope="class")
    def p(self) -> dict:
        return load_personality()

    def test_nothing_when_no_game(self, p):
        assert build_gaming_block(p, 9, game_active=False) == ""

    def test_nothing_when_disabled(self, p):
        cfg = {"gaming_awareness": {**p["gaming_awareness"], "enabled": False}}
        assert build_gaming_block(cfg, 9, game_active=True) == ""

    def test_never_leaks_infrastructure(self, p):
        block = build_gaming_block(p, 0, game_active=True)
        assert block.startswith("GAME IN PROGRESS")
        assert "Never mention GPUs, models, hardware" in block

    @pytest.mark.parametrize("level, tone", [
        (0, "Games are Mechty's territory"),
        (2, "Games are Mechty's territory"),
        (3, "Ask if he's winning"),
        (5, "Ask if he's winning"),
        (6, "play the next one with him"),
        (9, "play the next one with him"),
    ])
    def test_tone_follows_highest_reached_tier(self, p, level, tone):
        assert tone in build_gaming_block(p, level, game_active=True)

    def test_guidance_only_when_no_tier_reached(self):
        cfg = {"gaming_awareness": {"enabled": True, "guidance": "G.", "tiers": {5: "T."}}}
        assert build_gaming_block(cfg, 2, game_active=True) == "G."

    def test_empty_config_yields_nothing(self):
        assert build_gaming_block({"gaming_awareness": {"enabled": True}}, 9, game_active=True) == ""


class TestProactiveHoldsOffMidGame:
    _CFG = {"random_events": {"lore": {"min_affection": 0, "weight": 10, "messages": ["Status nominal."]}}}

    async def _tick(self, probe=None):
        e = ProactiveEngine()
        e._on_message_callback = AsyncMock()
        if probe is not None:
            e.set_game_active_probe(probe)
        with patch("app.proactive.events.now_local", return_value=datetime(2026, 1, 15, 12)), \
             patch("app.proactive.events.random.random", return_value=0.01), \
             patch("app.personality.load_personality", return_value=self._CFG):
            await e._random_event()
        return e

    async def test_no_random_event_while_gaming(self):
        probe = AsyncMock(return_value=True)
        e = await self._tick(probe)
        probe.assert_awaited_once()
        e._on_message_callback.assert_not_awaited()

    async def test_fires_when_not_gaming(self):
        e = await self._tick(AsyncMock(return_value=False))
        e._on_message_callback.assert_awaited_once_with("Status nominal.")

    async def test_fires_without_a_probe(self):
        e = await self._tick()
        e._on_message_callback.assert_awaited_once()
