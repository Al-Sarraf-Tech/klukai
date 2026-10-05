"""Every surface agrees on what she has on: image prompts, the wardrobe
block, and the context block's CURRENT OUTFIT / LOCATION lines."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from app.image_gen import build_prompt, build_mission_prompt, is_outfit_unlocked
from app.image_gen_constants import KLUKAI_DEFAULT_OUTFIT, KLUKAI_IDENTITY
from app.personality import load_personality
from app.personality.moods import build_context_block
from app.personality.state_blocks import build_wardrobe_block


class TestImagePrompt:
    def test_todays_outfit_drives_the_render(self):
        prompt = build_prompt("standing on the deck", costume="cerulean_breaker")
        assert "black bikini" in prompt and "surfboard under arm" in prompt

    def test_legacy_id_renders_the_canon_gown(self):
        assert "wedding dress" in build_prompt("chapel", costume="starlit_vow")

    def test_explicit_bath_scene_outranks_the_outfit_at_eight(self):
        prompt = build_prompt("onsen", context="draw us in the bath", costume="immaculate_service",
                              affection_level=8, request="draw us in the bath")
        assert "maid" not in prompt
        assert "steam" in prompt

    def test_intimate_outfits_never_leak_below_eight(self):
        prompt = build_prompt("onsen", context="draw us in the bath", costume="immaculate_service",
                              affection_level=7, request="draw us in the bath")
        assert "maid headdress" in prompt and "towel" not in prompt
        # No costume: the keyword fallback is gated too.
        for ctx in ("good morning sleepyhead", "Bed. That's an order.", "underwear"):
            p = build_prompt("portrait", context=ctx, affection_level=1)
            assert KLUKAI_DEFAULT_OUTFIT in p, ctx

    def test_only_his_words_make_it_a_bed_scene(self):
        prompt = build_prompt("selfie", context="Bed. That's an order.\nsend me a selfie",
                              costume="blazing_star", affection_level=9, request="send me a selfie")
        assert "baseball cap" in prompt and "lingerie" not in prompt

    def test_lighting_words_no_longer_strip_her(self):
        # "morning" used to swap in the bare-legs shirt; today's outfit stands.
        prompt = build_prompt("good morning portrait", context="good morning", costume="blazing_star")
        assert "baseball cap" in prompt
        assert "no pants" not in prompt

    def test_unknown_costume_falls_back_to_keywords(self):
        assert KLUKAI_DEFAULT_OUTFIT in build_prompt("portrait", costume="not_real")

    def test_identity_carries_her_marks_and_no_fixed_hairstyle(self):
        assert "teardrop facial mark" in KLUKAI_IDENTITY
        assert "cross hair ornament" in KLUKAI_IDENTITY
        assert "ponytail" not in KLUKAI_IDENTITY
        assert "ponytail" in KLUKAI_DEFAULT_OUTFIT
        assert "ponytail" in build_mission_prompt()

    def test_unlock_delegates_to_the_catalog(self):
        assert is_outfit_unlocked("speed_star", 0)
        assert not is_outfit_unlocked("indigo_oath", 7)
        assert is_outfit_unlocked("starlit_vow", 8)
        assert not is_outfit_unlocked("bogus", 9)


@pytest.fixture(scope="module")
def p() -> dict:
    return load_personality()


class TestWardrobeBlock:
    def test_originals_are_one_compact_line(self, p):
        block = build_wardrobe_block(p, 5)
        kit = [ln for ln in block.splitlines() if ln.startswith("- Everyday kit:")]
        assert len(kit) == 1
        assert "Night-Ride Jacket" in kit[0] and "Winter Patrol" in kit[0]
        assert "- Night-Ride Jacket:" not in block

    def test_denied_outfit_never_listed(self, p):
        assert "Klukadile" not in build_wardrobe_block(p, 9)

    def test_only_originals(self):
        block = build_wardrobe_block({"costumes": {"x": {"name": "X Kit", "source": "original"}}}, 0)
        assert block.endswith("- Everyday kit: X Kit.")


class TestContextBlock:
    def _block(self, **kw):
        with patch("app.personality.moods.now_local", return_value=datetime(2026, 10, 5, 15, 0)):
            return build_context_block("composed", 5, 10, **kw)

    def test_her_day_lines_replace_the_defaults(self):
        block = self._block(current_outfit="Hangar Coveralls — grease.", current_location="Hangar — tuning.")
        assert "CURRENT OUTFIT: Hangar Coveralls — grease." in block
        assert "LOCATION: The Elmo (Mobile Base Vehicle) — Hangar — tuning." in block

    def test_defaults_without_her_day(self):
        block = self._block()
        assert "CURRENT OUTFIT: Blazing Star tactical gear." in block
        assert "command deck or private quarters depending on time" in block


class TestSfwBackstop:
    def test_negative_prompt(self):
        from app.image_gen import negative_prompt
        from app.image_gen_constants import NEGATIVE_TAGS

        assert negative_prompt() == NEGATIVE_TAGS
        sfw = negative_prompt(sfw=True)
        assert sfw.startswith(NEGATIVE_TAGS) and "nude" in sfw and "topless" in sfw

    @pytest.mark.asyncio
    async def test_sfw_negative_reaches_the_workflow(self):
        from unittest.mock import MagicMock

        import app.image_gen as ig

        captured = {}

        class _Resp:
            status_code = 500

            def json(self):
                return {}

        async def post(url, json=None, **_):
            captured["workflow"] = json
            return _Resp()

        client = MagicMock()
        client.post = post
        with patch.object(ig, "_get_http", return_value=client), \
             patch.object(ig, "_lease_headers", return_value={}, create=True):
            try:
                await ig._try_generate("p", 832, 1216, MagicMock(), sfw=True)
            except Exception:
                pass
        wf = captured.get("workflow") or {}
        prompt = wf.get("prompt", wf)
        assert "topless" in prompt["7"]["inputs"]["text"]

    def test_originals_spell_out_their_inner_layers(self):
        from app import wardrobe

        cat = wardrobe.catalog()
        for oid in ("winter_patrol", "red_scarf_sortie", "dress_uniform"):
            assert "buttoned" in cat[oid].image_tags, oid
        assert "zipped onesie" in cat["klukadile_pajamas"].image_tags
        assert "pajama pants" in cat["sleepless_watch"].image_tags
        assert "navel" not in KLUKAI_IDENTITY
