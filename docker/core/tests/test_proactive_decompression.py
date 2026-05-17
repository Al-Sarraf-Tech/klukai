"""Tests for proactive ProactiveEngine._decompression_message + mission lifecycle helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.proactive import MissionTimer, ProactiveEngine


class TestDecompressionMessage:
    @pytest.mark.asyncio
    async def test_no_callback_silent(self):
        e = ProactiveEngine()
        e._on_message_callback = None
        # Should not raise — skips sleep + skips delivery via short-circuit
        with patch("app.proactive.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.random.uniform", return_value=0):
            await e._decompression_message("alice", had_injury=False, update_count=0)

    @pytest.mark.asyncio
    async def test_injury_uses_injury_messages(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 0
        with patch("app.proactive.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.random.uniform", return_value=0):
            await e._decompression_message("alice", had_injury=True, update_count=2)
        cb.assert_awaited_once()
        message = cb.call_args.args[0]
        # Injury messages reference wound / bandage / scared / sting / wince
        assert any(
            kw in message.lower()
            for kw in ("wound", "bandage", "scared", "stings", "wincing", "medic", "hurt", "got back")
        )

    @pytest.mark.asyncio
    async def test_long_mission_uses_exhaustion_messages(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 0
        with patch("app.proactive.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.random.uniform", return_value=0):
            await e._decompression_message("alice", had_injury=False, update_count=6)
        cb.assert_awaited_once()
        message = cb.call_args.args[0]
        # Exhaustion messages reference tired / sitting / coffee
        assert any(kw in message.lower() for kw in ("tired", "shaking", "coffee", "long one", "exhausted", "rest"))

    @pytest.mark.asyncio
    async def test_short_mission_normal_decompression(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 0
        with patch("app.proactive.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.random.uniform", return_value=0):
            await e._decompression_message("alice", had_injury=False, update_count=2)
        cb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_high_affection_adds_intimate_addon(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 8  # >= 7 threshold
        with patch("app.proactive.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.random.uniform", return_value=0):
            await e._decompression_message("alice", had_injury=False, update_count=2)
        cb.assert_awaited_once()
        message = cb.call_args.args[0]
        # Intimate addons include "Stay close", "lean into", "take your hand"
        assert any(kw in message.lower() for kw in ("close", "lean", "hand", "stay"))

    @pytest.mark.asyncio
    async def test_increments_proactive_count(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        before_count = e._proactive_count_today
        with patch("app.proactive.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.random.uniform", return_value=0):
            await e._decompression_message("alice", had_injury=False, update_count=2)
        assert e._proactive_count_today == before_count + 1


class TestStopMission:
    @pytest.mark.asyncio
    async def test_no_timer_silent(self):
        e = ProactiveEngine()
        # No mission active — stop should not raise
        e.stop_mission("alice", trigger_aftermath=False)
        # No state change
        assert "alice" not in e._mission_timers

    @pytest.mark.asyncio
    async def test_inactive_timer_silent(self):
        e = ProactiveEngine()
        with patch.object(MissionTimer, "_tick_loop", AsyncMock()):
            timer = MissionTimer()
            timer.active = False
            e._mission_timers["alice"] = timer
        # Stop on inactive timer doesn't run aftermath
        e.stop_mission("alice", trigger_aftermath=False)
        # Timer removed
        assert "alice" not in e._mission_timers
