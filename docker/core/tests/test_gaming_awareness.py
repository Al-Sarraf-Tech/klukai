"""Gaming awareness: Klukai knows when the Commander is mid-game (ADR-0018).

The router's game-active probe feeds a per-message prompt block and holds
proactive random events off while a game owns the GPU host. Since Klukai went
cloud-only (2026-10-26) the probe has no source and answers False.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.llm_router import LLMRouter
from app.personality import build_gaming_block, load_personality
from app.proactive import ProactiveEngine


class TestRouterGameActive:
    async def test_answers_false_without_any_probe(self):
        # The only source was the Dominus model gateway, which Klukai no longer
        # uses (cloud AI only); nothing is asked over the network.
        r = LLMRouter()
        with patch("httpx.AsyncClient") as client:
            assert await r.is_game_active() is False
        client.assert_not_called()


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
