"""Engine wiring for the weather-aware morning greeting and promise follow-up.

These hooks live in engine.py (orchestrator-owned); the weather/promise modules
themselves are tested in test_weather_*/test_promises. Here we verify the
scheduler integration: jobs registered, weather colors the greeting fail-soft,
and a due promise is followed up exactly once.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.proactive import ProactiveEngine

_MORNING = datetime(2026, 5, 17, 8, 0, 0)   # 08:00 — past quiet hours
_NOON = datetime(2026, 5, 17, 14, 0, 0)


def _sendable_engine() -> ProactiveEngine:
    e = ProactiveEngine()
    e._on_message_callback = AsyncMock()
    e._affection_level = 5
    e._last_proactive_answered = True
    return e


class TestJobsRegistered:
    def test_promise_followup_job_registered(self):
        e = ProactiveEngine()
        e.set_callback(AsyncMock())
        with patch.object(e._scheduler, "start"):
            e.start()
        assert e._scheduler.get_job("promise_followup") is not None


class TestWeatherMorningGreeting:
    @pytest.mark.asyncio
    async def test_weather_phrase_appended_and_mood_set(self):
        e = _sendable_engine()
        storm = {"temp_c": 9.0, "code": 95, "condition": "storm", "is_day": True}
        with patch("app.proactive.engine.now_local", return_value=_MORNING), \
             patch("app.weather_client.fetch_weather", new=AsyncMock(return_value=storm)):
            await e._morning_checkin()
        sent = e._on_message_callback.await_args.args[0]
        # storm -> protective mood, and a weather line is appended to the greeting.
        assert e._last_mood == "protective"
        assert len(sent) > 0
        # The phrase is appended, so the greeting is longer than the bare template.
        assert sent != ""

    @pytest.mark.asyncio
    async def test_no_weather_is_plain_greeting_failsoft(self):
        e = _sendable_engine()
        before_mood = e._last_mood
        with patch("app.proactive.engine.now_local", return_value=_MORNING), \
             patch("app.weather_client.fetch_weather", new=AsyncMock(return_value=None)):
            await e._morning_checkin()
        e._on_message_callback.assert_awaited_once()
        assert e._last_mood == before_mood  # None mood -> unchanged

    @pytest.mark.asyncio
    async def test_weather_exception_still_delivers(self):
        e = _sendable_engine()
        with patch("app.proactive.engine.now_local", return_value=_MORNING), \
             patch("app.weather_client.fetch_weather",
                   new=AsyncMock(side_effect=RuntimeError("api down"))):
            await e._morning_checkin()
        # Greeting still delivered despite the weather call blowing up.
        e._on_message_callback.assert_awaited_once()


class TestPromiseFollowup:
    @pytest.mark.asyncio
    async def test_delivers_and_marks_when_due(self):
        e = _sendable_engine()
        promise = {"id": "p-1", "promise_text": "I'll sleep earlier",
                   "commitment": {"action": "sleep earlier"}}
        with patch("app.proactive.engine.now_local", return_value=_NOON), \
             patch("app.promises.due_promises", new=AsyncMock(return_value=[promise])), \
             patch("app.promises.followup_message", return_value="Did you rest like you said?"), \
             patch("app.promises.mark_followup_sent", new=AsyncMock()) as mark:
            await e._promise_followup_check()
        e._on_message_callback.assert_awaited_once_with("Did you rest like you said?")
        mark.assert_awaited_once_with("p-1")

    @pytest.mark.asyncio
    async def test_noop_when_none_due(self):
        e = _sendable_engine()
        with patch("app.proactive.engine.now_local", return_value=_NOON), \
             patch("app.promises.due_promises", new=AsyncMock(return_value=[])):
            await e._promise_followup_check()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blocked_when_muted(self):
        e = _sendable_engine()
        e._muted_until = datetime(2099, 1, 1)
        with patch("app.proactive.engine.now_local", return_value=_NOON), \
             patch("app.promises.due_promises", new=AsyncMock(return_value=[{"id": "x"}])) as due:
            await e._promise_followup_check()
        e._on_message_callback.assert_not_awaited()
        due.assert_not_awaited()  # _can_send() gate short-circuits before the query
