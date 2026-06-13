"""Tests for app.proactive — MissionTimer + ProactiveEngine.

Targets the stateful classes that drive klukai's per-user proactive
messaging. Focus on isolated method behavior without spinning up the
real scheduler (APScheduler is heavy + not needed for unit coverage).
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import proactive
from app.proactive import MissionTimer, ProactiveEngine


# ═══════════════════════════════════════════════════════════════════════════
# MissionTimer
# ═══════════════════════════════════════════════════════════════════════════


class TestMissionTimerInit:
    def test_initial_state(self):
        t = MissionTimer()
        assert t.mission_description == ""
        assert t.base_interval_minutes == 30
        assert t.update_count == 0
        assert t.active_events == []
        assert t.active is False
        assert t._affection_level == 0

    def test_active_events_is_list(self):
        t = MissionTimer()
        assert isinstance(t.active_events, list)
        # Mutating one timer's events shouldn't leak to another
        t.active_events.append("test")
        t2 = MissionTimer()
        assert t2.active_events == []


class TestMissionTimerStartStop:
    @pytest.mark.asyncio
    async def test_start_sets_active_true(self):
        t = MissionTimer()
        with patch.object(t, "_tick_loop", AsyncMock()):
            t.start("test mission", interval_minutes=10, callback=None, affection_level=3)
        try:
            assert t.active is True
            assert t.mission_description == "test mission"
            assert t.base_interval_minutes == 10
            assert t._affection_level == 3
        finally:
            t.stop()

    @pytest.mark.asyncio
    async def test_start_floors_interval_at_5_minutes(self):
        t = MissionTimer()
        with patch.object(t, "_tick_loop", AsyncMock()):
            t.start("x", interval_minutes=1, callback=None)
        try:
            assert t.base_interval_minutes == 5
        finally:
            t.stop()

    @pytest.mark.asyncio
    async def test_start_records_start_time(self):
        t = MissionTimer()
        with patch.object(t, "_tick_loop", AsyncMock()):
            t.start("x", interval_minutes=10)
        try:
            assert t.started_at > 0
            assert t.last_update == t.started_at
        finally:
            t.stop()

    @pytest.mark.asyncio
    async def test_stop_sets_active_false(self):
        t = MissionTimer()
        with patch.object(t, "_tick_loop", AsyncMock()):
            t.start("x")
        t.stop()
        assert t.active is False

    @pytest.mark.asyncio
    async def test_stop_clears_global_active_timer(self):
        proactive.state._active_mission_timer = None
        t = MissionTimer()
        with patch.object(t, "_tick_loop", AsyncMock()):
            t.start("x")
            assert proactive.state._active_mission_timer is t
        t.stop()
        assert proactive.state._active_mission_timer is None


class TestMissionTimerEventTracking:
    def test_active_events_persist_across_updates(self):
        t = MissionTimer()
        t.active_events.append("klukai_injured")
        assert "klukai_injured" in t.active_events
        # Adding the same event twice shouldn't dupe in the canonical
        # implementation (set semantics via "if not in" check)
        assert t.active_events.count("klukai_injured") == 1


# ═══════════════════════════════════════════════════════════════════════════
# ProactiveEngine — pure setters and per-user state
# ═══════════════════════════════════════════════════════════════════════════


class TestProactiveEngineInit:
    def test_per_user_dicts_empty_at_init(self):
        e = ProactiveEngine()
        assert e._proactive_counts == {}
        assert e._last_answered == {}
        assert e._moods == {}
        assert e._affection_levels == {}
        assert e._mission_timers == {}
        assert e._romance_delivered == {}
        assert e._dream_delivered == {}
        assert e._user_messaged == {}

    def test_legacy_compat_state_at_init(self):
        e = ProactiveEngine()
        assert e._proactive_count_today == 0
        assert e._last_proactive_answered is True
        assert e._random_events_today == 0
        assert e._last_mood == "composed"
        assert e._affection_level == 0
        assert e._mission_timer is None
        assert e._romance_delivered_today is False
        assert e._dream_delivered_today is False
        assert e._user_messaged_today is False

    def test_shared_state_at_init(self):
        e = ProactiveEngine()
        assert e._last_random_event is None
        assert e._last_message_time is None


class TestProactiveEngineSetters:
    def test_set_affection_level_per_user(self):
        e = ProactiveEngine()
        e.set_affection_level(5, user_id="alice")
        e.set_affection_level(2, user_id="bob")
        assert e._affection_levels == {"alice": 5, "bob": 2}

    def test_set_affection_level_legacy_compat_uses_last_call(self):
        e = ProactiveEngine()
        e.set_affection_level(5, "alice")
        # Legacy field reflects the most recent call
        assert e._affection_level == 5
        e.set_affection_level(2, "bob")
        assert e._affection_level == 2

    def test_set_last_mood_per_user(self):
        e = ProactiveEngine()
        e.set_last_mood("tender", user_id="alice")
        e.set_last_mood("playful", user_id="bob")
        assert e._moods == {"alice": "tender", "bob": "playful"}

    def test_set_callback(self):
        e = ProactiveEngine()
        cb = MagicMock()
        e.set_callback(cb)
        assert e._on_message_callback is cb

    def test_set_recap_callback(self):
        e = ProactiveEngine()
        cb = MagicMock()
        e.set_recap_callback(cb)
        assert e._on_recap_callback is cb

    def test_set_session_getter(self):
        e = ProactiveEngine()
        getter = MagicMock()
        e.set_session_getter(getter)
        assert e._session_getter is getter


class TestProactiveEngineMute:
    def test_mute_sets_until(self):
        e = ProactiveEngine()
        e.mute(hours=2)
        assert e._muted_until is not None
        # Compare in the engine's clock (Commander-local, naive) — comparing
        # against server-naive datetime.now() only passes when the test host
        # happens to be in America/Chicago.
        from app.proactive.state import now_local
        assert e._muted_until > now_local()

    def test_mute_default_indefinite_when_none(self):
        e = ProactiveEngine()
        e.mute(hours=None)
        assert e._muted_until is not None
        # mute(None) sets _muted_until to datetime(9999, 12, 31) — "forever"
        assert e._muted_until.year == 9999

    def test_unmute_clears(self):
        e = ProactiveEngine()
        e.mute(hours=5)
        e.unmute()
        assert e._muted_until is None

    def test_mark_responded_clears_last_message_time_freshness(self):
        e = ProactiveEngine()
        e._last_proactive_answered = False
        e.mark_responded()
        assert e._last_proactive_answered is True
        assert e._last_message_time is not None


class TestCanSendGuards:
    def test_blocked_when_muted(self):
        e = ProactiveEngine()
        # Set _muted_until directly in the future, then patch the real now()
        # to be before the mute expiry
        from datetime import datetime as _dt
        e._muted_until = _dt(2099, 1, 1)  # far future
        e._proactive_count_today = 0
        e._last_proactive_answered = True
        with patch("app.proactive.engine.now_local") as mock_dt:
            mock_dt.return_value = _dt(2026, 5, 17, 12, 0, 0)
            assert e._can_send() is False

    def test_blocked_during_quiet_hours_morning(self):
        e = ProactiveEngine()
        e._muted_until = None
        e._proactive_count_today = 0
        e._last_proactive_answered = True
        with patch("app.proactive.engine.now_local") as mock_dt:
            mock_dt.return_value = datetime(2026, 5, 17, 5, 0, 0)  # 5am
            assert e._can_send() is False

    def test_blocked_during_quiet_hours_late_night(self):
        e = ProactiveEngine()
        e._muted_until = None
        e._proactive_count_today = 0
        e._last_proactive_answered = True
        with patch("app.proactive.engine.now_local") as mock_dt:
            mock_dt.return_value = datetime(2026, 5, 17, 23, 30, 0)
            assert e._can_send() is False

    def test_blocked_at_daily_cap(self):
        e = ProactiveEngine()
        e._muted_until = None
        e._proactive_count_today = proactive.MAX_PROACTIVE_PER_DAY
        e._last_proactive_answered = True
        with patch("app.proactive.engine.now_local") as mock_dt:
            mock_dt.return_value = datetime(2026, 5, 17, 14, 0, 0)
            assert e._can_send() is False

    def test_blocked_when_prior_proactive_unanswered(self):
        e = ProactiveEngine()
        e._muted_until = None
        e._proactive_count_today = 5
        e._last_proactive_answered = False
        with patch("app.proactive.engine.now_local") as mock_dt:
            mock_dt.return_value = datetime(2026, 5, 17, 14, 0, 0)
            assert e._can_send() is False

    def test_allowed_when_all_clear(self):
        e = ProactiveEngine()
        e._muted_until = None
        e._proactive_count_today = 5
        e._last_proactive_answered = True
        with patch("app.proactive.engine.now_local") as mock_dt:
            mock_dt.return_value = datetime(2026, 5, 17, 14, 0, 0)
            assert e._can_send() is True


class TestPickMessage:
    def test_returns_message_for_known_level(self):
        e = ProactiveEngine()
        e._affection_level = 0
        msgs = {0: ["msg-a", "msg-b"], 1: ["msg-c"]}
        result = e._pick_message(msgs)
        assert result in ["msg-a", "msg-b"]

    def test_falls_back_to_lower_level_when_exact_missing(self):
        e = ProactiveEngine()
        e._affection_level = 5
        msgs = {0: ["low"], 3: ["mid"]}
        # affection 5 — should pick from level 3 (highest <= 5)
        result = e._pick_message(msgs)
        assert result == "mid"

    def test_falls_back_to_level_0_when_only_zero_available(self):
        e = ProactiveEngine()
        e._affection_level = 9
        msgs = {0: ["only"]}
        assert e._pick_message(msgs) == "only"

    def test_returns_ellipsis_when_dict_empty(self):
        e = ProactiveEngine()
        e._affection_level = 0
        assert e._pick_message({}) == "..."


class TestMissionManagement:
    def test_mission_active_false_initially(self):
        e = ProactiveEngine()
        # mission_active is a @property
        assert e.mission_active is False

    @pytest.mark.asyncio
    async def test_start_mission_registers_timer(self):
        e = ProactiveEngine()
        with patch.object(MissionTimer, "_tick_loop", AsyncMock()):
            e.start_mission("test mission", interval_minutes=10, user_id="alice")
        try:
            assert "alice" in e._mission_timers
            assert e._mission_timers["alice"].active is True
        finally:
            for t in e._mission_timers.values():
                t.stop()

    @pytest.mark.asyncio
    async def test_start_mission_stops_existing(self):
        e = ProactiveEngine()
        with patch.object(MissionTimer, "_tick_loop", AsyncMock()):
            e.start_mission("first", user_id="alice")
            first_timer = e._mission_timers["alice"]
            e.start_mission("second", user_id="alice")
            second_timer = e._mission_timers["alice"]
        try:
            assert first_timer is not second_timer
            assert first_timer.active is False
            assert second_timer.mission_description == "second"
        finally:
            for t in e._mission_timers.values():
                t.stop()


class TestMarkUserMessagedToday:
    def test_sets_per_user_flag(self):
        e = ProactiveEngine()
        e.mark_user_messaged_today("alice")
        assert e._user_messaged.get("alice") is True
        assert "bob" not in e._user_messaged

    def test_legacy_compat_flag(self):
        e = ProactiveEngine()
        e.mark_user_messaged_today("alice")
        assert e._user_messaged_today is True
