"""_handle_message wiring for Command Decisions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app import decisions as dc
from tests.test_chat_handlers_coverage import _drain_tasks, _fresh_session, _patched_pipeline

NOW = datetime(2026, 10, 5, 19, 40, tzinfo=timezone.utc)
PENDING = {
    "id": "point_tomorrow",
    "ask": "Who takes point tomorrow? A: Belka. B: Andoris.",
    "options": [
        {"key": "A", "label": "Belka", "keywords": ["belka"], "consequence": "Belka took point.",
         "effect": {"fact": {"key": "her:point_call", "value": "Belka on point"}}},
        {"key": "B", "label": "Andoris", "keywords": ["andoris"], "consequence": "Andoris took point."},
    ],
    "asked_at": (NOW - timedelta(hours=2)).isoformat(),
}


@pytest.fixture(autouse=True)
def _clear_caches():
    dc._pending_cache.clear()
    dc._her_cache.clear()
    yield
    dc._pending_cache.clear()
    dc._her_cache.clear()


async def _run(content: str, *, pending=None, her=None, **pipeline_kw):
    from app.chat_handlers import _handle_message

    captured: dict = {}

    async def _stream(system_prompt, messages, config):
        captured["sys"] = system_prompt
        yield "Ok."

    with _patched_pipeline(**pipeline_kw) as ns:
        ns.router.stream = _stream
        ns.memory.recall_fact = AsyncMock(return_value=json.dumps(pending) if pending else None)
        ns.memory.recall_facts_by_pattern = AsyncMock(return_value=her or [])
        ns.memory.store_fact = AsyncMock()
        with patch("app.chat_handlers.router", ns.router), \
             patch("app.night_watch.now_local", return_value=datetime(2026, 10, 5, 14, 40)), \
             patch("app.decisions._utcnow", return_value=NOW), \
             patch("app.deferred.schedule", new=AsyncMock(return_value="t")) as sched:
            await _handle_message(content, _fresh_session())
            await _drain_tasks()
    return captured["sys"], ns, sched


class TestDecisionWiring:
    @pytest.mark.asyncio
    async def test_answer_resolves_with_a_one_shot_block(self):
        prompt, ns, sched = await _run("Andoris, obviously", pending=PENDING, aff_level=4,
                                       birthday_block="COMMANDER'S BIRTHDAY: today")
        assert "COMMAND DECISION" in prompt and "B: Andoris" in prompt
        assert prompt.endswith("COMMANDER'S BIRTHDAY: today")
        sched.assert_awaited_once()
        assert sched.await_args.kwargs["user_id"] == "default"

    @pytest.mark.asyncio
    async def test_unrelated_message_leaves_it_pending(self):
        prompt, ns, sched = await _run("long day at work today", pending=PENDING, aff_level=4)
        assert "COMMAND DECISION" not in prompt
        sched.assert_not_awaited()
        ns.memory.store_fact.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_her_world_block_at_three_plus(self):
        her = [{"key": "companion:default:her:bike_exhaust", "value": "titanium, his call"}]
        prompt, _, _ = await _run("how's the bike?", her=her, aff_level=3)
        assert "HER WORLD" in prompt and "bike exhaust: titanium, his call" in prompt
        dc._her_cache.clear()
        low, _, _ = await _run("how's the bike?", her=her, aff_level=2)
        assert "HER WORLD" not in low
