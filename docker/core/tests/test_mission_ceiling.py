"""A mission she started on her own stands down on its own.

Regression guards for the auto-mission ceiling: without it a battle_ready
mood started a mission that ticked every ~30 minutes until the cancel phrase
or a restart, and a reconnect restored a mission from half a day ago.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import SessionState
from app.proactive.engine import ProactiveEngine
from app.proactive.mission import AUTO_MISSION_MAX_MINUTES, MissionTimer


class TestMissionTimerCeiling:
    @pytest.mark.asyncio
    async def test_first_tick_past_the_ceiling_stands_down_without_firing(self):
        callback = AsyncMock()
        on_expire = MagicMock()
        timer = MissionTimer()
        # The timer's tick loop lives in mission_timer.py (split from mission.py).
        with patch("app.proactive.mission_timer.asyncio.sleep", AsyncMock()):
            timer.start(
                "night op", interval_minutes=5, callback=callback,
                max_duration_minutes=0, on_expire=on_expire,
            )
            await timer._task
        assert timer.active is False
        assert timer.update_count == 0
        callback.assert_not_awaited()
        on_expire.assert_called_once_with()

    def test_no_ceiling_means_never_past_it(self):
        timer = MissionTimer()
        timer.started_at = 0.0
        timer.max_duration_minutes = None
        assert timer._past_ceiling() is False

    def test_ceiling_is_measured_from_start(self):
        timer = MissionTimer()
        timer.max_duration_minutes = 180
        # _past_ceiling reads the clock in mission_timer.py (split from mission.py).
        with patch("app.proactive.mission_timer.time.monotonic", return_value=1000.0):
            timer.started_at = 1000.0 - 179 * 60
            assert timer._past_ceiling() is False
            timer.started_at = 1000.0 - 180 * 60
            assert timer._past_ceiling() is True


class TestEngineWiring:
    @pytest.mark.asyncio
    async def test_start_mission_with_ceiling_closes_out_like_a_cancel(self):
        e = ProactiveEngine()
        e.trigger_mission_aftermath_image = AsyncMock()
        e._decompression_message = AsyncMock()
        e._set_post_mission_physical = AsyncMock()
        with patch.object(MissionTimer, "_tick_loop", AsyncMock()):
            e.start_mission("auto op", user_id="alice", max_duration_minutes=180)
        timer = e._mission_timers["alice"]
        try:
            assert timer.max_duration_minutes == 180
            assert timer._on_expire is not None
            timer._on_expire()  # what the tick loop calls at the ceiling
            assert timer.active is False
            assert "alice" not in e._mission_timers
            assert e.mission_active is False
        finally:
            for t in e._mission_timers.values():
                t.stop()

    @pytest.mark.asyncio
    async def test_explicit_mission_has_no_ceiling(self):
        e = ProactiveEngine()
        with patch.object(MissionTimer, "_tick_loop", AsyncMock()):
            e.start_mission("keep me posted", interval_minutes=30, user_id="alice")
        try:
            timer = e._mission_timers["alice"]
            assert timer.max_duration_minutes is None
            assert timer._on_expire is None
        finally:
            for t in e._mission_timers.values():
                t.stop()


class TestAutoStartPassesTheCeiling:
    @pytest.mark.asyncio
    async def test_battle_ready_mission_is_ceilinged(self):
        from app.background import _maybe_auto_start_mission

        proactive = MagicMock()
        proactive.mission_active = False
        memory = MagicMock()
        memory.save_session = AsyncMock()
        session = SessionState(conversation_id="c1", mood="battle_ready")
        session.turns = [{"role": "user", "content": "contact front"}]
        with patch("app.background.proactive", proactive), patch("app.background.memory", memory):
            await _maybe_auto_start_mission("battle_ready", "composed", session, "u1")
        proactive.start_mission.assert_called_once_with(
            "contact front", interval_minutes=30, max_duration_minutes=AUTO_MISSION_MAX_MINUTES,
        )
        assert AUTO_MISSION_MAX_MINUTES == 180


class TestStaleMissionNotRestored:
    def test_staleness_rules(self):
        from app.chat import MISSION_RESTORE_MAX_HOURS, _mission_is_stale

        assert _mission_is_stale(None) is False
        assert _mission_is_stale("") is False
        assert _mission_is_stale("not a date") is False
        fresh = (datetime.now() - timedelta(hours=1)).isoformat()
        old = (datetime.now() - timedelta(hours=MISSION_RESTORE_MAX_HOURS + 1)).isoformat()
        assert _mission_is_stale(fresh) is False
        assert _mission_is_stale(old) is True

    @pytest.mark.asyncio
    async def test_reconnect_clears_a_half_day_old_mission_instead_of_restoring(self):
        from tests.test_chat_coverage import (
            _FakeConn,
            _FakeWebSocket,
            _capture_endpoint,
            _fake_ws_manager,
        )

        endpoint, _ = _capture_endpoint()
        sock = _FakeWebSocket(token="good")
        ws = _fake_ws_manager()

        existing = SessionState(
            conversation_id="c1",
            mood="composed",
            mission_description="patrol",
            mission_interval=20,
            mission_started_at=(datetime.now() - timedelta(hours=13)).isoformat(),
        )
        memory = MagicMock()
        memory.get_session = AsyncMock(return_value=existing)
        memory.save_session = AsyncMock()

        proactive = MagicMock()
        proactive.mission_active = False
        proactive.start_mission = MagicMock()
        affection = MagicMock()
        affection.get_state = AsyncMock(return_value=MagicMock(level=6))

        with patch("app.chat.ws", ws), patch("app.chat.memory", memory), patch(
            "app.chat.proactive", proactive
        ), patch("app.chat.affection", affection), patch(
            "app.auth.get_user_from_token", AsyncMock(return_value="u1")
        ), patch(
            "app.chat.get_conn", return_value=_FakeConn(("composed",))
        ), patch(
            "app.chat._maybe_reflect_on_return", AsyncMock()
        ):
            await endpoint(sock)

        proactive.start_mission.assert_not_called()
        assert existing.mission_description is None
        assert existing.mission_interval is None
        assert existing.mission_started_at is None
        memory.save_session.assert_awaited()
