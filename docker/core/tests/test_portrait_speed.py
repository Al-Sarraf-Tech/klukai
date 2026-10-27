"""Live Portrait hooks: wardrobe picks and the scheduler still call the
portrait module, which never starts a render from them (on-demand policy).

The local inpaint pipeline these tests once covered (one GPU lease per set,
one ComfyUI graph, face-crop inpainting, the blink verifier) was removed with
the local renderer on 2026-10-26: pictures are cloud-only.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════════
# Hooks: wardrobe picks and the scheduler
# ═══════════════════════════════════════════════════════════════════════════


class TestHooks:
    @pytest.mark.asyncio
    async def test_connect_warm_up_prewarms(self):
        from app import context, wardrobe
        aff = MagicMock()
        aff.get_state = AsyncMock(return_value=SimpleNamespace(level=5))
        with patch.object(context, "affection", aff), \
             patch("app.wardrobe.ensure_today", new=AsyncMock()), \
             patch("app.portrait.prewarm_soon") as soon:
            await wardrobe.warm_today("claude")
        soon.assert_called_once_with("claude", 5)

    @pytest.mark.asyncio
    async def test_pwa_pick_prewarms(self):
        from app import wardrobe
        row = SimpleNamespace(persisted=True, day=None, requested_outfit_id=None)
        with patch("app.wardrobe.ensure_today", new=AsyncMock(return_value=row)), \
             patch("app.wardrobe._record_request", new=AsyncMock(return_value=True)), \
             patch("app.wardrobe.replace", side_effect=lambda r, **kw: r), \
             patch("app.portrait.prewarm_soon") as soon:
            await wardrobe.set_requested("claude", "speed_star", 4)
        soon.assert_called_once_with("claude", 4)

    @pytest.mark.asyncio
    async def test_accepted_chat_request_prewarms(self):
        from app import wardrobe
        row = SimpleNamespace(persisted=True, day=None, request_changes=0, requested_outfit_id=None)
        outcome = SimpleNamespace(decision="comply")
        with patch("app.wardrobe.ensure_today", new=AsyncMock(return_value=row)), \
             patch("app.wardrobe.catalog", return_value={"speed_star": MagicMock()}), \
             patch("app.wardrobe.decide_request", return_value=outcome), \
             patch("app.wardrobe.occasions_for", new=AsyncMock(return_value=frozenset())), \
             patch("app.wardrobe._record_request", new=AsyncMock(return_value=True)), \
             patch("app.wardrobe.replace", side_effect=lambda r, **kw: r), \
             patch("app.portrait.prewarm_soon") as soon:
            assert await wardrobe.handle_request("claude", "speed_star", 4, current_id="x") is outcome
        soon.assert_called_once_with("claude", 4)

    @pytest.mark.asyncio
    async def test_backfill_job_is_registered_and_never_replayed(self):
        from app.proactive import durability as dur
        from app.proactive.engine import ProactiveEngine
        e = ProactiveEngine()
        try:
            e.start()
            job = e._scheduler.get_job("portrait_backfill")
            fields = {f.name: str(f) for f in job.trigger.fields}
            assert (fields["hour"], fields["minute"]) == ("2-6", "*/15")
        finally:
            e.stop()
        assert "portrait_backfill" in dur.NEVER_CATCH_UP

    @pytest.mark.asyncio
    async def test_backfill_job_delegates_for_the_primary_user(self):
        from app.decisions import PRIMARY_USER
        from app.proactive.engine import ProactiveEngine
        with patch("app.portrait.backfill_tick", new=AsyncMock()) as tick:
            await ProactiveEngine()._portrait_backfill()
        tick.assert_awaited_once_with(PRIMARY_USER)
