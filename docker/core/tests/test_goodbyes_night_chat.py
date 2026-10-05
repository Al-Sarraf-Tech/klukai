"""_handle_message wiring for Clean Goodbyes.

Reuses the fully-patched chat pipeline from test_chat_handlers_coverage so the
only real code under test is the per-message block wiring.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.test_chat_handlers_coverage import _drain_tasks, _fresh_session, _patched_pipeline


async def _run(content: str, **pipeline_kw):
    from app.chat_handlers import _handle_message

    captured: dict = {}

    async def _stream(system_prompt, messages, config):
        captured["sys"] = system_prompt
        yield "Ok."

    with _patched_pipeline(**pipeline_kw) as ns:
        ns.router.stream = _stream
        with patch("app.chat_handlers.router", ns.router):
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
