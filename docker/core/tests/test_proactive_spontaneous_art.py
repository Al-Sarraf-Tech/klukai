"""Spontaneous art — Klukai draws something for the Commander unprompted, saves
it to the album as a lasting (kept) gift, and shows it to him live if connected.

Rare, bonded-only, tender by design, and fully fail-open.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.proactive import events as ev
from app.proactive.engine import ProactiveEngine


def _engine(aff: int = 9):
    e = ProactiveEngine()
    e._affection_level = aff
    e._last_mood = "tender"
    e._last_spontaneous_art = None
    e._muted_until = None
    e._on_message_callback = AsyncMock()
    return e


def _ws(connected: bool = True):
    w = MagicMock()
    w.is_connected = MagicMock(return_value=connected)
    w.send = AsyncMock()
    return w


def _gen_patches(img=b"IMGBYTES", save=None):
    return (
        patch("app.image_gen.generate_image", new=AsyncMock(return_value=img)),
        patch("app.image_gen.build_prompt", new=MagicMock(return_value="PROMPT")),
        patch("app.memory_archive.save_image",
              new=(save or AsyncMock(return_value="mem-id"))),
        patch.object(ev.asyncio, "sleep", new=AsyncMock()),
    )


class TestSpontaneousArt:
    @pytest.mark.asyncio
    async def test_draws_saves_and_delivers_when_connected(self):
        e = _engine()
        ws = _ws(connected=True)
        gen, bp, save, slp = _gen_patches()
        with gen as g, bp, save as s, slp, patch("app.context.ws", ws):
            await e._spontaneous_art_event()
        g.assert_awaited_once()
        s.assert_awaited_once()                                   # persisted to album
        assert s.await_args.kwargs["curation"]["keep"] is True
        assert s.await_args.kwargs["curation"]["category"] == "Precious Memories"
        e._on_message_callback.assert_awaited_once()              # told him
        ws.send.assert_awaited_once()                             # showed him the image
        assert ws.send.await_args.args[1]["type"] == "image"
        assert e._last_spontaneous_art is not None                # cooldown armed

    @pytest.mark.asyncio
    async def test_saves_but_silent_when_not_connected(self):
        e = _engine()
        ws = _ws(connected=False)
        gen, bp, save, slp = _gen_patches()
        with gen, bp, save as s, slp, patch("app.context.ws", ws):
            await e._spontaneous_art_event()
        s.assert_awaited_once()                                   # still lands in album
        e._on_message_callback.assert_not_awaited()               # no live announce
        ws.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_not_bonded(self):
        e = _engine(aff=4)
        gen, bp, save, slp = _gen_patches()
        with gen as g, bp, save as s, slp, patch("app.context.ws", _ws()):
            await e._spontaneous_art_event()
        g.assert_not_awaited()
        s.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cooldown_blocks_repeat(self):
        e = _engine()
        e._last_spontaneous_art = datetime.now() - timedelta(hours=10)  # < 60h
        gen, bp, save, slp = _gen_patches()
        with gen as g, bp, save as s, slp, patch("app.context.ws", _ws()):
            await e._spontaneous_art_event()
        g.assert_not_awaited()
        s.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_muted_blocks(self):
        e = _engine()
        e._muted_until = datetime.now() + timedelta(hours=1)
        gen, bp, save, slp = _gen_patches()
        with gen as g, bp, save as s, slp, patch("app.context.ws", _ws()):
            await e._spontaneous_art_event()
        g.assert_not_awaited()
        s.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_save_when_generation_fails(self):
        e = _engine()
        gen, bp, save, slp = _gen_patches(img=None)
        with gen, bp, save as s, slp, patch("app.context.ws", _ws()):
            await e._spontaneous_art_event()
        s.assert_not_awaited()
        assert e._last_spontaneous_art is None

    @pytest.mark.asyncio
    async def test_save_failure_is_swallowed(self):
        e = _engine()
        boom = AsyncMock(side_effect=RuntimeError("db down"))
        gen, bp, save, slp = _gen_patches(save=boom)
        with gen, bp, save, slp, patch("app.context.ws", _ws()):
            await e._spontaneous_art_event()                      # must not raise
        assert e._last_spontaneous_art is None                    # cooldown not armed

    @pytest.mark.asyncio
    async def test_tick_rolls_gate(self):
        e = _engine()
        with patch.object(ev.random, "random", return_value=0.9), \
             patch.object(e, "_spontaneous_art_event", new=AsyncMock()) as evt:
            await e._spontaneous_art_tick()
            evt.assert_not_awaited()                              # > 0.18 → skip
        with patch.object(ev.random, "random", return_value=0.05), \
             patch.object(e, "_spontaneous_art_event", new=AsyncMock()) as evt:
            await e._spontaneous_art_tick()
            evt.assert_awaited_once()                             # <= 0.18 → fire

    def test_pieces_fallback_shape(self):
        pieces = ev._spontaneous_art_pieces()
        assert len(pieces) >= 3
        assert all({"scene", "annotation", "message"} <= set(p) for p in pieces)

    def test_pieces_uses_yaml_when_valid(self):
        good = [{"scene": "s", "annotation": "a", "message": "m"}]
        with patch.object(ev, "_raw_content", return_value=good):
            assert ev._spontaneous_art_pieces() == good
        # Malformed YAML (missing keys) falls back to the literal.
        with patch.object(ev, "_raw_content", return_value=[{"scene": "only"}]):
            assert ev._spontaneous_art_pieces() is ev._SPONTANEOUS_ART_PIECES_FALLBACK
