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
        with patch("app.proactive.mission_timer.asyncio.sleep", AsyncMock()):
            timer.start(
                "night op",
                interval_minutes=5,
                callback=callback,
                max_duration_minutes=0,
                on_expire=on_expire,
            )
            await timer._task
        assert timer.active is False
        assert timer.update_count == 0
        callback.assert_not_awaited()
        on_expire.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_on_expire_sees_the_mission_still_active(self):
        """The engine closes out only an active mission, so the timer must
        not mark itself inactive before calling on_expire."""
        seen: list[bool] = []
        timer = MissionTimer()
        with patch("app.proactive.mission_timer.asyncio.sleep", AsyncMock()):
            timer.start(
                "night op",
                max_duration_minutes=0,
                on_expire=lambda: seen.append(timer.active),
            )
            await timer._task
        assert seen == [True]
        assert timer.active is False

    def test_no_ceiling_means_never_past_it(self):
        timer = MissionTimer()
        timer.started_at = 0.0
        timer.max_duration_minutes = None
        assert timer._past_ceiling() is False

    def test_ceiling_is_measured_from_start(self):
        timer = MissionTimer()
        timer.max_duration_minutes = 180
        with patch("app.proactive.mission_timer.time.monotonic", return_value=1000.0):
            timer.started_at = 1000.0 - 179 * 60
            assert timer._past_ceiling() is False
            timer.started_at = 1000.0 - 180 * 60
            assert timer._past_ceiling() is True


class TestEngineWiring:
    @pytest.mark.asyncio
    async def test_ceiling_closes_out_like_a_cancel(self):
        """A real tick loop at the ceiling triggers the aftermath image, the
        decompression line and the post-mission physical state."""
        e = ProactiveEngine()
        e.trigger_mission_aftermath_image = AsyncMock()
        e._decompression_message = AsyncMock()
        e._set_post_mission_physical = AsyncMock()
        with patch("app.proactive.mission_timer.asyncio.sleep", AsyncMock()):
            e.start_mission("auto op", user_id="alice", max_duration_minutes=0)
            timer = e._mission_timers["alice"]
            await timer._task
            for task in list(e._background_tasks):
                await task
        assert timer._task.cancelled() is False
        assert timer.active is False
        assert "alice" not in e._mission_timers
        assert e.get_mission_timer("alice") is None
        e.trigger_mission_aftermath_image.assert_awaited_once()
        e._decompression_message.assert_awaited_once_with("alice", False, 0)
        e._set_post_mission_physical.assert_awaited_once_with("alice", False)

    @pytest.mark.asyncio
    async def test_a_replaced_mission_is_not_closed_by_the_old_ceiling(self):
        e = ProactiveEngine()
        with patch.object(MissionTimer, "_tick_loop", AsyncMock()):
            e.start_mission("auto op", user_id="alice", max_duration_minutes=180)
            old = e._mission_timers["alice"]
            e.start_mission("keep me posted", user_id="alice")
        new = e._mission_timers["alice"]
        try:
            old._on_expire()
            assert e._mission_timers["alice"] is new
            assert new.active is True
        finally:
            new.stop()

    @pytest.mark.asyncio
    async def test_explicit_mission_has_no_ceiling(self):
        e = ProactiveEngine()
        with patch.object(MissionTimer, "_tick_loop", AsyncMock()):
            e.start_mission("keep me posted", interval_minutes=30, user_id="alice")
        timer = e._mission_timers["alice"]
        try:
            assert timer.max_duration_minutes is None
            assert timer._on_expire is None
        finally:
            timer.stop()


class TestAutoStartPassesTheCeiling:
    @pytest.mark.asyncio
    async def test_battle_ready_mission_is_ceilinged(self):
        from app import background

        snapshot = SessionState(conversation_id="c1", mood="composed")
        session = SessionState(conversation_id="c1", mood="composed")
        session.turns = [{"role": "user", "content": "contact front"}]
        memory = MagicMock()
        memory.session_owner = MagicMock(return_value=AsyncMock())
        memory.get_session = AsyncMock(return_value=session)
        memory.save_session = AsyncMock()
        proactive = MagicMock()
        proactive._moods = {}
        proactive.get_mission_timer = MagicMock(return_value=None)
        ws = MagicMock()
        ws.send_mood = AsyncMock()
        with patch.object(background, "proactive", proactive), patch.object(
            background, "memory", memory
        ), patch.object(background, "ws", ws), patch.object(
            background, "get_conn_autocommit", side_effect=RuntimeError("no db")
        ):
            applied = await background._apply_extracted_state(
                snapshot, "battle_ready", "u1"
            )
        assert applied is True
        proactive.start_mission.assert_called_once_with(
            "contact front",
            interval_minutes=30,
            user_id="u1",
            max_duration_minutes=AUTO_MISSION_MAX_MINUTES,
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
        aware_old = (
            datetime.now().astimezone() - timedelta(hours=MISSION_RESTORE_MAX_HOURS + 1)
        ).isoformat()
        assert _mission_is_stale(aware_old) is True

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
        proactive.get_mission_timer = MagicMock(return_value=None)
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
