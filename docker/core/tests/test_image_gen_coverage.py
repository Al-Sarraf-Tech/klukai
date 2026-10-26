"""Behavioral coverage for app.image_gen: prompt builders and the cloud dispatch.

Pictures are cloud-only (owner decision 2026-10-26). ``generate_image`` sends
each render to the account worker (``agy``, the default) or the opt-in OpenAI
provider, and any other provider value generates nothing. The account-worker
path itself is covered in tests/test_agy_image_provider.py and the OpenAI path
in tests/test_image_provider.py; this file pins the dispatch and the prompt
helpers re-exported here.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

import app.image_gen as ig
from app.image_gen import build_mission_prompt


# ═══════════════════════════════════════════════════════════════════════════
# build_mission_prompt — injury branches
# ═══════════════════════════════════════════════════════════════════════════


class TestMissionPromptInjuries:
    def test_squad_injured_tag_when_squad_present(self):
        prompt = build_mission_prompt(
            scene_type="combat",
            squad_members=["mechty"],
            injuries=["squad_injured"],
        )
        assert "injured teammate" in prompt
        assert "supporting each other" in prompt

    def test_medical_emergency_tag_when_squad_present(self):
        prompt = build_mission_prompt(
            scene_type="combat",
            squad_members=["mechty"],
            injuries=["medical_emergency"],
        )
        assert "field medic" in prompt
        assert "treating wounds" in prompt

    def test_squad_injury_tags_skipped_without_squad(self):
        # The squad-injury branches live under `if squad_members:` — with no
        # squad they must not fire even when the injury flags are set.
        prompt = build_mission_prompt(
            scene_type="combat",
            squad_members=[],
            injuries=["squad_injured", "medical_emergency"],
        )
        assert "injured teammate" not in prompt
        assert "field medic" not in prompt


def test_build_prompt_can_drop_the_affection_tags():
    from app.image_gen_constants import AFFECTION_MOOD_TAGS

    with_tags = ig.build_prompt("upper body", affection_level=4)
    without = ig.build_prompt("upper body", affection_level=4, affection_tags=False)
    assert AFFECTION_MOOD_TAGS[4] in with_tags
    assert AFFECTION_MOOD_TAGS[4] not in without
    assert without.endswith("upper body")


def test_negative_prompt_appends_extras_after_the_sfw_backstop():
    assert ig.negative_prompt(True, "cyan hair").endswith(
        f"{ig.SFW_NEGATIVE_TAGS}, cyan hair"
    )
    assert ig.negative_prompt(False, None) == ig.NEGATIVE_TAGS
    assert ig.negative_prompt(False, "") == ig.NEGATIVE_TAGS


def test_public_exports_resolve_and_carry_no_local_renderer():
    for name in ig.__all__:
        assert hasattr(ig, name), name
    for gone in (
        "check_comfyui_ready",
        "free_comfyui_vram",
        "generate_img2img",
        "generate_inpaint_set",
        "COMFYUI_URL",
        "WORKFLOW_TEMPLATE",
        "Sampling",
        "InpaintBranch",
        "close",
    ):
        assert not hasattr(ig, gone), gone


# ═══════════════════════════════════════════════════════════════════════════
# generate_image — provider dispatch
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_openai_provider_forwards_the_render_options(monkeypatch):
    from app import image_provider

    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "openai")
    with patch.object(
        image_provider, "generate_cloud_image", new=AsyncMock(return_value=b"PNG")
    ) as cloud:
        result = await ig.generate_image(
            "a scene", 1216, 832, sfw=True, negative_extra="red hair"
        )
    assert result == b"PNG"
    cloud.assert_awaited_once_with(
        "a scene", 1216, 832, sfw=True, negative_extra="red hair"
    )


@pytest.mark.asyncio
async def test_default_dimensions_are_portrait(monkeypatch):
    from app import image_provider

    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "openai")
    with patch.object(
        image_provider, "generate_cloud_image", new=AsyncMock(return_value=None)
    ) as cloud:
        assert await ig.generate_image("a scene") is None
    assert cloud.await_args.args[1:] == (832, 1216)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["comfyui", "disabled", "openia", ""])
async def test_any_other_provider_generates_nothing(monkeypatch, caplog, provider):
    from app import agy_image_provider, image_provider

    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", provider)
    cloud = AsyncMock(side_effect=AssertionError("cloud render"))
    worker = AsyncMock(side_effect=AssertionError("worker render"))
    monkeypatch.setattr(image_provider, "generate_cloud_image", cloud)
    monkeypatch.setattr(agy_image_provider, "submit_agy_image", worker)
    with caplog.at_level(logging.WARNING, logger="app.image_gen"):
        assert await ig.generate_image("a scene", user_id="jalsarraf") is None
    assert "Unknown image provider" in caplog.text
    cloud.assert_not_awaited()
    worker.assert_not_awaited()


@pytest.mark.asyncio
async def test_account_worker_refuses_an_invalid_description(monkeypatch):
    from app import agy_image_provider

    monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "agy")
    monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", "u1")
    worker = AsyncMock(side_effect=AssertionError("invalid brief submitted"))
    monkeypatch.setattr(agy_image_provider, "submit_agy_image", worker)
    assert await ig.generate_image("   ", user_id="u1") is None
    worker.assert_not_awaited()
