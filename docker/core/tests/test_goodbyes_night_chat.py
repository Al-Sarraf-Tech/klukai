"""_handle_message wiring for Clean Goodbyes and the 0200 Watch.

Reuses the fully-patched chat pipeline from test_chat_handlers_coverage so the
only real code under test is the per-message block wiring.
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from tests.test_chat_handlers_coverage import _drain_tasks, _fresh_session, _patched_pipeline

_DAY = datetime(2026, 10, 4, 14, 0)
_NIGHT = datetime(2026, 10, 5, 2, 10)


async def _run(content: str, *, at: datetime = _DAY, late_nights=None, **pipeline_kw):
    from app.chat_handlers import _handle_message

    captured: dict = {}

    async def _stream(system_prompt, messages, config):
        captured["sys"] = system_prompt
        yield "Ok."

    with _patched_pipeline(**pipeline_kw) as ns:
        ns.router.stream = _stream
        ns.memory.recall_fact = AsyncMock(
            return_value=json.dumps(late_nights) if late_nights is not None else None
        )
        ns.memory.store_fact = AsyncMock()
        with patch("app.chat_handlers.router", ns.router), \
             patch("app.night_watch.now_local", return_value=at):
            await _handle_message(content, _fresh_session())
            await _drain_tasks()
    return captured["sys"], ns


class TestGoodbyeWiring:
    @pytest.mark.asyncio
    async def test_goodnight_appends_departure_and_holds_pings_until_morning(self):
        prompt, ns = await _run("goodnight, Klukai", aff_level=7)
        assert "DEPARTURE" in prompt
        assert "I'll be here." in prompt
        ns.proactive.mark_goodnight.assert_called_once_with()
        ns.proactive.mark_leaving.assert_not_called()
        # Activity tracking still ran first (it clears any earlier hold).
        ns.proactive.mark_responded.assert_called_once()

    @pytest.mark.asyncio
    async def test_leave_appends_departure_and_holds_ninety_minutes(self):
        prompt, ns = await _run("gotta go, sorry!", aff_level=1)
        assert "DEPARTURE" in prompt
        assert "Dismissed." in prompt
        ns.proactive.mark_leaving.assert_called_once_with()
        ns.proactive.mark_goodnight.assert_not_called()

    @pytest.mark.asyncio
    async def test_ordinary_message_has_no_departure(self):
        prompt, ns = await _run("How was patrol this afternoon?", aff_level=7)
        assert "DEPARTURE" not in prompt
        ns.proactive.mark_goodnight.assert_not_called()
        ns.proactive.mark_leaving.assert_not_called()

    @pytest.mark.asyncio
    async def test_birthday_block_still_lands_last(self):
        prompt, _ = await _run("bye!", birthday_block="COMMANDER'S BIRTHDAY: today")
        assert "DEPARTURE" in prompt
        assert prompt.endswith("COMMANDER'S BIRTHDAY: today")


class TestNightWatchWiring:
    @pytest.mark.asyncio
    async def test_night_watch_block_at_0210(self):
        prompt, ns = await _run("can't sleep", at=_NIGHT, aff_level=4)
        assert "NIGHT WATCH (0210 hours" in prompt
        ns.memory.store_fact.assert_awaited_once()  # tonight recorded

    @pytest.mark.asyncio
    async def test_no_night_watch_by_day(self):
        prompt, ns = await _run("can't sleep", at=_DAY, aff_level=4)
        assert "NIGHT WATCH" not in prompt
        ns.memory.recall_fact.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_streak_orders_him_to_bed(self):
        prompt, _ = await _run(
            "still up", at=_NIGHT, aff_level=4, late_nights=["2026-10-03", "2026-10-04"],
        )
        assert "Bed. That's an order." in prompt

    @pytest.mark.asyncio
    async def test_goodnight_at_0200_gets_both_blocks(self):
        prompt, ns = await _run("ok goodnight", at=_NIGHT, aff_level=6,
                                birthday_block="COMMANDER'S BIRTHDAY: today")
        assert "DEPARTURE" in prompt and "NIGHT WATCH" in prompt
        assert prompt.index("DEPARTURE") < prompt.index("NIGHT WATCH")
        assert prompt.endswith("COMMANDER'S BIRTHDAY: today")
        ns.proactive.mark_goodnight.assert_called_once_with()
