"""Tests for proactive ProactiveEngine._decompression_message + mission lifecycle helpers."""

from __future__ import annotations

import contextlib
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.proactive import MAX_PROACTIVE_PER_DAY, MissionTimer, ProactiveEngine

# A "safe" send window: 15:00 local, outside quiet hours (23:00-08:00). The
# decompression tests freeze the clock here so the now-live _can_send() gate
# (mute / quiet-hours / daily-cap) is evaluated deterministically rather than
# against the wall clock at test time.
_AFTERNOON = datetime(2026, 5, 17, 15, 0, 0)

_DECOMP_NOW_TARGETS = (
    "app.proactive.engine.now_local",
    "app.proactive.mission.now_local",
)


@contextlib.contextmanager
def _freeze(value: datetime):
    """Freeze now_local() across the modules the decompression gate reads."""
    mock_dt = MagicMock(return_value=value)
    with contextlib.ExitStack() as stack:
        for target in _DECOMP_NOW_TARGETS:
            stack.enter_context(patch(target, mock_dt))
        yield mock_dt


class TestDecompressionMessage:
    @pytest.mark.asyncio
    async def test_no_callback_silent(self):
        e = ProactiveEngine()
        e._on_message_callback = None
        # Should not raise — skips sleep + skips delivery via short-circuit
        with patch("app.proactive.mission.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.mission.random.uniform", return_value=0):
            await e._decompression_message("alice", had_injury=False, update_count=0)

    @pytest.mark.asyncio
    async def test_injury_uses_injury_messages(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 0
        with _freeze(_AFTERNOON), \
             patch("app.proactive.mission.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.mission.random.uniform", return_value=0):
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
        with _freeze(_AFTERNOON), \
             patch("app.proactive.mission.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.mission.random.uniform", return_value=0):
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
        with _freeze(_AFTERNOON), \
             patch("app.proactive.mission.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.mission.random.uniform", return_value=0):
            await e._decompression_message("alice", had_injury=False, update_count=2)
        cb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_high_affection_adds_intimate_addon(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 8  # >= 7 threshold
        with _freeze(_AFTERNOON), \
             patch("app.proactive.mission.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.mission.random.uniform", return_value=0):
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
        with _freeze(_AFTERNOON), \
             patch("app.proactive.mission.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.mission.random.uniform", return_value=0):
            await e._decompression_message("alice", had_injury=False, update_count=2)
        assert e._proactive_count_today == before_count + 1


class TestDecompressionGating:
    """The post-mission decompression message must respect the same send gate
    (mute / quiet-hours / daily-cap) as every other proactive send — it must not
    bypass it just because a mission ended."""

    @pytest.mark.asyncio
    async def test_muted_blocks_decompression(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._muted_until = datetime(2099, 1, 1)  # muted far into the future
        before = e._proactive_count_today
        with _freeze(_AFTERNOON), \
             patch("app.proactive.mission.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.mission.random.uniform", return_value=0):
            await e._decompression_message("alice", had_injury=False, update_count=2)
        cb.assert_not_awaited()
        # A blocked send must not consume the daily proactive budget either.
        assert e._proactive_count_today == before

    @pytest.mark.asyncio
    async def test_quiet_hours_block_decompression(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        # 02:00 local is inside quiet hours (23:00-08:00) — no send.
        with _freeze(datetime(2026, 5, 17, 2, 0, 0)), \
             patch("app.proactive.mission.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.mission.random.uniform", return_value=0):
            await e._decompression_message("alice", had_injury=False, update_count=2)
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_daily_cap_blocks_decompression(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._proactive_count_today = MAX_PROACTIVE_PER_DAY  # cap already hit
        with _freeze(_AFTERNOON), \
             patch("app.proactive.mission.asyncio.sleep", new=AsyncMock()), \
             patch("app.proactive.mission.random.uniform", return_value=0):
            await e._decompression_message("alice", had_injury=False, update_count=2)
        cb.assert_not_awaited()
        # Counter stays pinned at the cap — no overshoot.
        assert e._proactive_count_today == MAX_PROACTIVE_PER_DAY


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
