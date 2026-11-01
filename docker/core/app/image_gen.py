"""Image generation: prompts for every renderer, and the cloud dispatch.

Pictures are cloud-only (owner decision 2026-10-26): the account worker
(``KLUKAI_IMAGE_PROVIDER=agy``, the default) or the opt-in OpenAI provider
(``openai``). There is no local renderer, GPU gate or lease; any other
provider value disables generation.
"""

from __future__ import annotations

import logging
from typing import Any

# Constants extracted to image_gen_constants.py (S+ Phase 2 file-size hygiene).
# Imported here and re-exported for backward compat with callers that still
# `from app.image_gen import <CONSTANT>` (helpers.py, tests, etc.).
from app.image_gen_constants import (
    AFFECTION_MOOD_TAGS as AFFECTION_MOOD_TAGS,
    COMMANDER_DEFAULT_OUTFIT as COMMANDER_DEFAULT_OUTFIT,
    COMMANDER_IDENTITY as COMMANDER_IDENTITY,
    COMMANDER_OUTFIT_MAP as COMMANDER_OUTFIT_MAP,
    COUPLE_KEYWORDS as COUPLE_KEYWORDS,
    COUPLE_TAGS as COUPLE_TAGS,
    IMAGE_KEYWORDS as IMAGE_KEYWORDS,
    KLUKAI_DEFAULT_OUTFIT as KLUKAI_DEFAULT_OUTFIT,
    KLUKAI_IDENTITY as KLUKAI_IDENTITY,
    KLUKAI_LORA_TRIGGER as KLUKAI_LORA_TRIGGER,
    LANDSCAPE_KEYWORDS as LANDSCAPE_KEYWORDS,
    MISSION_SCENE_TAGS as MISSION_SCENE_TAGS,
    MOOD_EXPRESSION_TAGS as MOOD_EXPRESSION_TAGS,
    NEGATIVE_TAGS as NEGATIVE_TAGS,
    OUTFIT_MAP as OUTFIT_MAP,
    INTIMATE_MIN_LEVEL as INTIMATE_MIN_LEVEL,
    INTIMATE_OUTFIT_KEYS as INTIMATE_OUTFIT_KEYS,
    QUALITY_TAGS as QUALITY_TAGS,
    SCENE_OUTFIT_KEYWORDS as SCENE_OUTFIT_KEYWORDS,
    SFW_NEGATIVE_TAGS as SFW_NEGATIVE_TAGS,
    SITUATION_KEYWORDS as SITUATION_KEYWORDS,
    SQUAD_KEYWORDS as SQUAD_KEYWORDS,
    TIME_OF_DAY_TAGS as TIME_OF_DAY_TAGS,
)
from .image_prompts import (
    detect_squad_members as detect_squad_members,
    build_mission_prompt as build_mission_prompt,
    needs_image as needs_image,
    is_couple_scene as is_couple_scene,
    is_landscape as is_landscape,
    _select_outfit as _select_outfit,
    is_outfit_unlocked as is_outfit_unlocked,
    _explicit_scene as _explicit_scene,
    _outfit_map_for as _outfit_map_for,
    build_prompt as build_prompt,
    negative_prompt as negative_prompt,
)

# Public re-exports — keep linter happy AND preserve `from app.image_gen import X` paths.
__all__ = (
    "AFFECTION_MOOD_TAGS COMMANDER_DEFAULT_OUTFIT COMMANDER_IDENTITY "
    "COMMANDER_OUTFIT_MAP COUPLE_KEYWORDS COUPLE_TAGS IMAGE_KEYWORDS "
    "KLUKAI_DEFAULT_OUTFIT KLUKAI_IDENTITY KLUKAI_LORA_TRIGGER "
    "LANDSCAPE_KEYWORDS MISSION_SCENE_TAGS MOOD_EXPRESSION_TAGS NEGATIVE_TAGS "
    "OUTFIT_MAP INTIMATE_MIN_LEVEL INTIMATE_OUTFIT_KEYS QUALITY_TAGS "
    "SCENE_OUTFIT_KEYWORDS SFW_NEGATIVE_TAGS SITUATION_KEYWORDS SQUAD_KEYWORDS "
    "TIME_OF_DAY_TAGS build_mission_prompt build_prompt detect_squad_members "
    "generate_image is_couple_scene is_landscape is_outfit_unlocked "
    "needs_image negative_prompt"
).split()

logger = logging.getLogger(__name__)


async def generate_image(
    prompt: str,
    width: int = 832,
    height: int = 1216,
    *,
    sfw: bool = False,
    negative_extra: str | None = None,
    user_id: str | None = None,
    include_commander: bool = False,
) -> bytes | None:
    """Generate through the configured cloud scene provider (default: the
    account worker).

    ``sfw=True`` (wardrobe-driven renders below the intimacy gate) asks the
    renderer to keep every character covered; ``negative_extra`` appends a
    caller's own exclusions (Live Portrait's palette guard).

    Each call makes at most one bounded attempt; cloud allowance failures
    propagate to batch callers. An unknown provider generates nothing.
    """
    from . import image_provider

    provider = image_provider.selected_provider()
    if provider == "agy":
        from . import account_images
        from .agy_image_provider import submit_agy_image, uses_agy_images

        if not uses_agy_images(user_id):
            return None
        try:
            description = image_provider.cloud_prompt(
                prompt, sfw=sfw, negative_extra=negative_extra
            )
        except image_provider.ImageProviderError:
            return None
        request: dict[str, Any] = {
            "user_id": user_id or "",
            "width": width,
            "height": height,
            "include_commander": include_commander,
            # Her own pictures (Her POV, album, mission aftermath) come here, and
            # nudity is only ever granted when his own words ask for it, which
            # the chat and API paths decide (image_provider.nudity_allowed). A
            # "bath" or "onsen" in a prompt she composed is not him asking.
            "nudity": False,
        }
        result = await submit_agy_image(description, **request)
        if (
            result.get("status") == "busy"
            and not account_images.in_background_render()
            and await account_images.wait_for_background_render()
        ):
            # Another picture (a background render, a chat picture, another
            # Her POV) held the worker's one slot; "busy" proves this was never
            # accepted, so it goes next, once.
            result = await submit_agy_image(description, **request)
        if result.get("status") == "completed":
            return result.get("image")
        # Her POV reports only that it failed. Nothing else records why.
        logger.warning("No picture for %s: status=%s", user_id, result.get("status"))
        return None
    if provider == "openai":
        return await image_provider.generate_cloud_image(
            prompt,
            width,
            height,
            sfw=sfw,
            negative_extra=negative_extra,
        )
    logger.warning("Unknown image provider; generation disabled")
    return None
