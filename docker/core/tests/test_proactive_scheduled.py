"""Tests for proactive scheduled job methods: _deliver, _morning_checkin,
_daily_challenge, _evening_checkin, _idle_check, _mission_report, trigger_tap.

Mocks the dependencies (ws, physical, personality, callback, publish_event)
to exercise the code paths in isolation.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.proactive import ProactiveEngine


class TestDeliver:
    @pytest.mark.asyncio
    async def test_blocked_when_can_send_false(self):
        e = ProactiveEngine()
        e._on_message_callback = AsyncMock()
        # Force quiet hours so can_send returns False
        with patch("app.proactive.engine.now_local") as mock_dt:
            mock_dt.return_value = datetime(2026, 5, 17, 5, 0, 0)
            await e._deliver("test")
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_callback_silent_noop(self):
        e = ProactiveEngine()
        e._on_message_callback = None
        # Even if can_send is true, no callback means just silently return
        with patch("app.proactive.engine.now_local") as mock_dt:
            mock_dt.return_value = datetime(2026, 5, 17, 14, 0, 0)
            await e._deliver("test")
        # No raise, no count change
        assert e._proactive_count_today == 0

    @pytest.mark.asyncio
    async def test_delivers_and_counts(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        with patch("app.proactive.engine.now_local") as mock_dt, \
             patch("app.proactive.engine.publish_event", new=AsyncMock()):
            mock_dt.return_value = datetime(2026, 5, 17, 14, 0, 0)
            await e._deliver("Klukai message")
        cb.assert_awaited_once_with("Klukai message")
        assert e._proactive_count_today == 1
        assert e._last_proactive_answered is False


class TestMorningCheckin:
    @pytest.mark.asyncio
    async def test_calls_deliver_with_morning_message(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 3
        # Mock the physical/ws so it doesn't blow up
        with patch("app.proactive.engine.now_local") as mock_dt, \
             patch("app.context.physical") as phys, \
             patch("app.context.ws") as ws, \
             patch("app.weather_client.fetch_weather", new=AsyncMock(return_value=None)), \
             patch("app.proactive.engine.publish_event", new=AsyncMock()):
            mock_dt.return_value = datetime(2026, 5, 17, 8, 0, 0)
            ws._connections = {}  # no connected users
            await e._morning_checkin()
        # The morning check is in quiet hours pre-8, but at 8 should fire
        # ...actually QUIET_HOUR_END=8 means 8 is the boundary; 8:00 == not quiet
        cb.assert_awaited_once()


class TestDailyChallenge:
    @pytest.mark.asyncio
    async def test_no_callback_silent_skip(self):
        e = ProactiveEngine()
        e._on_message_callback = None
        await e._daily_challenge()
        # No raise

    @pytest.mark.asyncio
    async def test_low_affection_skips(self):
        e = ProactiveEngine()
        e._on_message_callback = AsyncMock()
        e._affection_level = 1  # below threshold
        await e._daily_challenge()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_challenges_in_config_silent_skip(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 5
        with patch("app.proactive.engine.now_local", return_value=datetime(2026, 5, 17, 14, 0, 0)), \
             patch("app.personality.load_personality") as load:
            load.return_value = {"daily_challenges": {"challenges": []}}
            await e._daily_challenge()
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_issues_challenge(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 5
        # Afternoon (outside 23-08 quiet hours), nothing muting the engine.
        with patch("app.proactive.engine.now_local", return_value=datetime(2026, 5, 17, 14, 0, 0)), \
             patch("app.personality.load_personality") as load:
            load.return_value = {"daily_challenges": {"challenges": [
                {"type": "creative", "prompt": "Write a haiku."},
            ]}}
            await e._daily_challenge()
        cb.assert_awaited_once()
        assert e._proactive_count_today == 1  # counts against the daily cap

    @pytest.mark.asyncio
    async def test_quiet_hours_block_challenge(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 5
        with patch("app.proactive.engine.now_local", return_value=datetime(2026, 5, 17, 3, 0, 0)), \
             patch("app.personality.load_personality") as load:
            load.return_value = {"daily_challenges": {"challenges": [
                {"type": "creative", "prompt": "Write a haiku."},
            ]}}
            await e._daily_challenge()
        cb.assert_not_awaited()
        assert e._proactive_count_today == 0

    @pytest.mark.asyncio
    async def test_mute_blocks_challenge(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 5
        e._muted_until = datetime(2099, 1, 1)
        with patch("app.proactive.engine.now_local", return_value=datetime(2026, 5, 17, 14, 0, 0)), \
             patch("app.personality.load_personality") as load:
            load.return_value = {"daily_challenges": {"challenges": [
                {"type": "creative", "prompt": "Write a haiku."},
            ]}}
            await e._daily_challenge()
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_daily_cap_blocks_challenge(self):
        from app.proactive.state import MAX_PROACTIVE_PER_DAY
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 5
        e._proactive_count_today = MAX_PROACTIVE_PER_DAY
        with patch("app.proactive.engine.now_local", return_value=datetime(2026, 5, 17, 14, 0, 0)), \
             patch("app.personality.load_personality") as load:
            load.return_value = {"daily_challenges": {"challenges": [
                {"type": "creative", "prompt": "Write a haiku."},
            ]}}
            await e._daily_challenge()
        cb.assert_not_awaited()


class TestIdleCheck:
    @pytest.mark.asyncio
    async def test_calls_deliver(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        with patch("app.proactive.engine.now_local") as mock_dt, \
             patch("app.proactive.engine.publish_event", new=AsyncMock()):
            mock_dt.return_value = datetime(2026, 5, 17, 14, 0, 0)
            await e._idle_check()
        cb.assert_awaited_once()


class TestMissionReport:
    @pytest.mark.asyncio
    async def test_50pct_chance_skips(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        with patch("app.proactive.mission.random.random", return_value=0.99):  # above 0.5 = skip
            await e._mission_report()
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_50pct_chance_delivers(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        with patch("app.proactive.mission.random.random", return_value=0.1), \
             patch("app.proactive.engine.now_local") as mock_dt, \
             patch("app.proactive.engine.publish_event", new=AsyncMock()):
            mock_dt.return_value = datetime(2026, 5, 17, 14, 0, 0)
            await e._mission_report()
        cb.assert_awaited_once()


class TestTriggerTap:
    @pytest.mark.asyncio
    async def test_emits_response_via_callback(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 0
        with patch("app.proactive.engine.now_local") as mock_dt, \
             patch("app.proactive.engine.publish_event", new=AsyncMock()):
            mock_dt.return_value = datetime(2026, 5, 17, 14, 0, 0)
            await e.trigger_tap()
        cb.assert_awaited_once()
        text = cb.call_args.args[0]
        assert isinstance(text, str)
        assert len(text) > 0

    @pytest.mark.asyncio
    async def test_level_4_uses_tender_tap_lines(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 4
        with patch("app.proactive.engine.now_local") as mock_dt, \
             patch("app.proactive.engine.publish_event", new=AsyncMock()):
            mock_dt.return_value = datetime(2026, 5, 17, 14, 0, 0)
            await e.trigger_tap()
        cb.assert_awaited_once()
