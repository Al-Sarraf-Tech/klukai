"""Tests for the on-connect Level-9 Oath capstone trigger.

``maybe_deliver_oath`` fires the one-time "Oath Fulfilled" scene exactly once
ever (guarded by companion_firsts), and ``_maybe_oath_on_connect`` gates it on
level>=9 so a Commander already at the max tier — who reached lv9 before this
feature existed — still gets the moment on their next connect.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_OATH_CFG = {"affection": {"oath_fulfilled_scene": ["Stay.", "I chose you.", "Thank you."]}}


def _ws():
    ws = MagicMock()
    ws.send_proactive = AsyncMock()
    return ws


def _proactive(first_result: bool):
    prox = MagicMock()
    prox.record_first = AsyncMock(return_value=first_result)
    return prox


def _affection(level: int):
    aff = MagicMock()
    aff.get_state = AsyncMock(return_value=MagicMock(level=level))
    return aff


class TestMaybeDeliverOath:
    @pytest.mark.asyncio
    async def test_delivers_when_unfired_and_scene_present(self):
        from app import background
        ws, prox = _ws(), _proactive(True)
        with patch("app.background.load_personality", return_value=_OATH_CFG), \
             patch("app.background.ws", ws), \
             patch("app.background.proactive", prox), \
             patch("app.background.asyncio.sleep", new=AsyncMock()):
            delivered = await background.maybe_deliver_oath("jalsarraf")
        assert delivered is True
        prox.record_first.assert_awaited_once_with("jalsarraf", "oath_fulfilled")
        assert ws.send_proactive.await_count == 3  # one per scene line

    @pytest.mark.asyncio
    async def test_skips_when_already_fired(self):
        from app import background
        ws, prox = _ws(), _proactive(False)
        with patch("app.background.load_personality", return_value=_OATH_CFG), \
             patch("app.background.ws", ws), \
             patch("app.background.proactive", prox), \
             patch("app.background.asyncio.sleep", new=AsyncMock()):
            delivered = await background.maybe_deliver_oath("jalsarraf")
        assert delivered is False
        ws.send_proactive.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_not_consume_first_when_scene_missing(self):
        """A missing/empty oath scene must NOT burn the once-ever 'first'."""
        from app import background
        prox = _proactive(True)
        with patch("app.background.load_personality", return_value={"affection": {}}), \
             patch("app.background.proactive", prox), \
             patch("app.background.asyncio.sleep", new=AsyncMock()):
            delivered = await background.maybe_deliver_oath("jalsarraf")
        assert delivered is False
        prox.record_first.assert_not_awaited()


class TestOathOnConnect:
    @pytest.mark.asyncio
    async def test_fires_at_level_9(self):
        from app import reflect_helpers
        deliver = AsyncMock(return_value=True)
        with patch("app.reflect_helpers.affection", _affection(9)), \
             patch("app.background.maybe_deliver_oath", deliver):
            await reflect_helpers._maybe_oath_on_connect("jalsarraf")
        deliver.assert_awaited_once_with("jalsarraf")

    @pytest.mark.asyncio
    async def test_skips_below_level_9(self):
        from app import reflect_helpers
        deliver = AsyncMock(return_value=True)
        with patch("app.reflect_helpers.affection", _affection(5)), \
             patch("app.background.maybe_deliver_oath", deliver):
            await reflect_helpers._maybe_oath_on_connect("jalsarraf")
        deliver.assert_not_awaited()
