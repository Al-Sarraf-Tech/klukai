"""Chat turn ↔ Her Day: the prompt knows what she has on and where she is,
and an outfit request is decided before the reply is written."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from app import her_day as hd
from app import wardrobe as w
from tests.test_chat_handlers_coverage import _drain_tasks, _fresh_session, _patched_pipeline


def _now(outfit="blazing_star", source="auto"):
    return hd.HerNow(
        day=date(2026, 10, 5), hour=15, outfit=w.catalog()[outfit], outfit_source=source,
        outfit_reason="A duty day.", location="Hangar", activity="tuning the suspension",
        block=None, blocks=[],
    )


async def _turn(content, *, her_now, request_outcome=None, aff_level=5, ws_send_outfit=None):
    from app.chat_handlers import _handle_message

    captured: dict = {}

    async def _stream(system_prompt, messages, config):
        captured["sys"] = system_prompt
        yield "Ok."

    handle = AsyncMock(return_value=request_outcome)
    with _patched_pipeline(aff_level=aff_level) as ns:
        ns.router.stream = _stream
        ns.ws.send_outfit = ws_send_outfit or AsyncMock()
        with patch("app.chat_handlers.router", ns.router), \
             patch("app.her_day.her_now", her_now), \
             patch("app.wardrobe.handle_request", handle):
            import app.chat_handlers as ch

            await _handle_message(content, _fresh_session())
            await _drain_tasks()
            assemble_kwargs = ch.assemble_system_prompt.call_args.kwargs
    return captured["sys"], assemble_kwargs, handle, ns


class TestPromptKnowsHerDay:
    @pytest.mark.asyncio
    async def test_outfit_and_location_reach_the_prompt(self):
        her_now = AsyncMock(return_value=_now())
        _, kwargs, handle, _ = await _turn("How was your afternoon?", her_now=her_now)
        assert kwargs["current_outfit"].startswith("Blazing Star — ")
        assert kwargs["current_location"].startswith("Hangar — tuning the suspension.")
        handle.assert_not_awaited()
        # mood, game flag and mission are threaded through
        call = her_now.await_args
        assert call.args[1] == 5
        assert call.kwargs["game_active"] is False
        assert call.kwargs["mission"] is None

    @pytest.mark.asyncio
    async def test_her_day_failure_keeps_the_turn_alive(self):
        her_now = AsyncMock(side_effect=RuntimeError("db down"))
        sys_prompt, kwargs, _, _ = await _turn("hey", her_now=her_now)
        assert kwargs["current_outfit"] is None
        assert kwargs["current_location"] is None
        assert sys_prompt.startswith("SYS")


class TestOutfitRequestInChat:
    @pytest.mark.asyncio
    async def test_granted_request_changes_her_before_the_reply(self):
        her_now = AsyncMock(side_effect=[_now(), _now("immaculate_service", "commander")])
        outcome = w.RequestOutcome("immaculate_service", "comply", "Comply.")
        send_outfit = AsyncMock()
        sys_prompt, kwargs, handle, _ = await _turn(
            "Wear the maid outfit for me", her_now=her_now, request_outcome=outcome,
            ws_send_outfit=send_outfit,
        )
        handle.assert_awaited_once()
        assert handle.await_args.args[1:3] == ("immaculate_service", 5)
        assert handle.await_args.kwargs["current_id"] == "blazing_star"
        assert "WARDROBE REQUEST" in sys_prompt
        assert "you are now wearing Immaculate Service" in sys_prompt
        assert kwargs["current_outfit"].startswith("Immaculate Service — ")
        payload = send_outfit.await_args.args[1]
        assert payload["id"] == "immaculate_service"
        assert payload["source"] == "commander"

    @pytest.mark.asyncio
    async def test_refused_request_changes_nothing(self):
        her_now = AsyncMock(return_value=_now())
        outcome = w.RequestOutcome("indigo_oath", "not_today", "'...Not today.'")
        send_outfit = AsyncMock()
        sys_prompt, kwargs, _, _ = await _turn(
            "put on the wedding dress", her_now=her_now, request_outcome=outcome,
            ws_send_outfit=send_outfit,
        )
        assert "NOT changing" in sys_prompt
        assert her_now.await_count == 1
        send_outfit.assert_not_awaited()
        assert kwargs["current_outfit"].startswith("Blazing Star")


class TestWsOutfitFrame:
    @pytest.mark.asyncio
    async def test_send_outfit(self):
        from app.ws_manager import WSManager

        mgr = WSManager()
        mgr.send = AsyncMock()
        await mgr.send_outfit("u", {"id": "speed_star"})
        mgr.send.assert_awaited_once_with("u", {"type": "outfit", "outfit": {"id": "speed_star"}})


def test_request_block_from_real_catalog_round_trip():
    oid = w.detect_outfit_request("change into your rider suit")
    block = w.request_block(w.decide_request(
        w.catalog()[oid], 5, current_id="blazing_star", request_changes=0,
        occasions_today=frozenset(), hour=15,
    ))
    assert "Speed Star" in block and "you are now wearing" in block


class TestTeaTimeInChat:
    @pytest.mark.asyncio
    async def test_counter_duty_serves_his_order(self):
        block = hd.Block("midday", 12, 14, "tea_counter", "Lounge", "Tea Time counter duty")
        now = hd.HerNow(day=date(2026, 10, 5), hour=13, outfit=w.catalog()["blazing_star"],
                        outfit_source="auto", outfit_reason="A duty day.", location="Lounge",
                        activity="Tea Time counter duty", block=block, blocks=[block])
        sys_prompt, _, _, _ = await _turn("One Racing Calm, please", her_now=AsyncMock(return_value=now))
        assert "TEA TIME:" in sys_prompt
