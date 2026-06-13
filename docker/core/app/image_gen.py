"""Image generation via ComfyUI with NoobAI-XL (Illustrious) for anime-realistic scenes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid

import httpx

# Constants extracted to image_gen_constants.py (S+ Phase 2 file-size hygiene).
# Imported here and re-exported for backward compat with callers that still
# `from app.image_gen import <CONSTANT>` (helpers.py, tests, etc.).
from app.image_gen_constants import (
    AFFECTION_MOOD_TAGS,
    COMMANDER_DEFAULT_OUTFIT,
    COMMANDER_IDENTITY,
    COMMANDER_OUTFIT_MAP,
    COUPLE_KEYWORDS,
    COUPLE_TAGS,
    IMAGE_KEYWORDS,
    KLUKAI_DEFAULT_OUTFIT,
    KLUKAI_IDENTITY,
    KLUKAI_LORA,
    KLUKAI_LORA_TRIGGER,
    LANDSCAPE_KEYWORDS,
    MISSION_SCENE_TAGS,
    MOOD_EXPRESSION_TAGS,
    NEGATIVE_TAGS,
    OUTFIT_COSTUME_TAGS,
    OUTFIT_MAP,
    OUTFIT_UNLOCK_LEVELS,
    QUALITY_TAGS,
    SITUATION_KEYWORDS,
    SQUAD_KEYWORDS,
    TIME_OF_DAY_TAGS,
    WORKFLOW_TEMPLATE,
)

# Public re-exports — keep linter happy AND preserve `from app.image_gen import X` paths.
__all__ = [
    "AFFECTION_MOOD_TAGS",
    "COMMANDER_DEFAULT_OUTFIT",
    "COMMANDER_IDENTITY",
    "COMMANDER_OUTFIT_MAP",
    "COUPLE_KEYWORDS",
    "COUPLE_TAGS",
    "IMAGE_KEYWORDS",
    "KLUKAI_DEFAULT_OUTFIT",
    "KLUKAI_IDENTITY",
    "KLUKAI_LORA",
    "KLUKAI_LORA_TRIGGER",
    "LANDSCAPE_KEYWORDS",
    "MISSION_SCENE_TAGS",
    "MOOD_EXPRESSION_TAGS",
    "NEGATIVE_TAGS",
    "OUTFIT_COSTUME_TAGS",
    "OUTFIT_MAP",
    "OUTFIT_UNLOCK_LEVELS",
    "QUALITY_TAGS",
    "SITUATION_KEYWORDS",
    "SQUAD_KEYWORDS",
    "TIME_OF_DAY_TAGS",
    "WORKFLOW_TEMPLATE",
    "build_mission_prompt",
    "build_prompt",
    "check_comfyui_ready",
    "detect_squad_members",
    "generate_image",
    "is_couple_scene",
    "is_landscape",
    "is_outfit_unlocked",
    "needs_image",
]

logger = logging.getLogger(__name__)

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://host.docker.internal:8388")


_http: httpx.AsyncClient | None = None
_image_gen_lock = asyncio.Semaphore(1)  # Only one image gen at a time — prevents GPU overload


def _get_http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=180.0)
    return _http


# Character identity tags — Danbooru format for Animagine XL 3.1
# Core identity is FIXED (face, hair, body). Outfit is selected per-scene.

# Scene-appropriate outfits — matched by keyword in conversation context





# Squad member detection for multi-character scenes
# Rich visual profiles — weapon designation IS the character identity
# Lore-accurate squad visual profiles — sourced from Danbooru tags, IOP Wiki, official art
# Each T-Doll's weapon designation IS their identity

# ── Mission-aware image generation ────────────────────────────────────────



def detect_squad_members(text: str) -> list[str]:
    """Detect which squad members are mentioned in the text."""
    lower = text.lower()
    found = []
    for name in SQUAD_KEYWORDS:
        if name in lower:
            found.append(name)
    return found


def build_mission_prompt(
    scene_type: str = "combat",
    squad_members: list[str] | None = None,
    injuries: list[str] | None = None,
    affection_level: int = 0,
) -> str:
    """Build an image prompt for mission-context scenes.

    Args:
        scene_type: Key from MISSION_SCENE_TAGS.
        squad_members: List of squad member names to include.
        injuries: Active injury events (e.g., ["klukai_injured", "squad_injured"]).
        affection_level: 0-9, affects Klukai's expression.
    """
    parts = [QUALITY_TAGS, KLUKAI_LORA_TRIGGER]

    # Klukai is always in mission images
    parts.append(KLUKAI_IDENTITY)
    parts.append("tactical gear, body armor, combat vest, rifle, intense")

    # Add injury tags to Klukai if she's hurt
    if injuries and "klukai_injured" in injuries:
        parts.append("bandaged arm, torn sleeve, blood stains, determined expression, still commanding")

    # Add squad members
    if squad_members:
        for name in squad_members[:3]:  # Max 3 extra characters to avoid crowding
            if name in SQUAD_KEYWORDS:
                parts.append(SQUAD_KEYWORDS[name])
        # Add injury tags to generic squad if injured
        if injuries and "squad_injured" in injuries:
            parts.append("injured teammate, field bandages, supporting each other")
        if injuries and "medical_emergency" in injuries:
            parts.append("field medic, treating wounds, urgent medical attention")

    # Scene tags
    scene = MISSION_SCENE_TAGS.get(scene_type, MISSION_SCENE_TAGS["combat"])
    parts.append(scene)

    # Multi-girl tag if squad members present
    if squad_members:
        count = len(squad_members) + 1  # +1 for Klukai
        parts.append(f"{count}girls, multiple girls, group")

    return ", ".join(parts)

# Situational context tags — detect what's happening in the conversation



# NoobAI-XL (Illustrious) + Klukai IL LoRA workflow

# Expanded keyword detection for image requests

# Landscape scene keywords — use wider aspect ratio

# Mood-to-scene mapping for affection-aware prompt enhancement


def needs_image(message: str) -> bool:
    """Check if the message is requesting image generation."""
    lower = message.lower()
    return any(kw in lower for kw in IMAGE_KEYWORDS)


def is_couple_scene(text: str) -> bool:
    """Detect if the request is for a scene with both Klukai and the Commander."""
    import re
    lower = text.lower()
    return any(re.search(r'\b' + re.escape(kw) + r'\b', lower) for kw in COUPLE_KEYWORDS)


def is_landscape(text: str) -> bool:
    """Detect if the scene should use landscape aspect ratio."""
    lower = text.lower()
    return any(kw in lower for kw in LANDSCAPE_KEYWORDS)


def _select_outfit(context: str, outfit_map: dict[str, str], default: str) -> str:
    """Pick the best outfit from a map based on conversation context keywords."""
    lower = context.lower()
    for keyword, outfit in outfit_map.items():
        if keyword in lower:
            return outfit
    return default


def is_outfit_unlocked(costume: str, affection_level: int) -> bool:
    """Return whether ``costume`` is unlocked at the given affection level.

    "Unlocked" is DERIVED on read — there is no wardrobe table. An unknown
    costume id is treated as locked (fail-closed). Outfits with an unlock
    level of 0 are always available.
    """
    if costume not in OUTFIT_UNLOCK_LEVELS:
        return False
    return affection_level >= OUTFIT_UNLOCK_LEVELS[costume]


def build_prompt(
    scene_tags: str,
    couple: bool = False,
    affection_level: int = 0,
    context: str = "",
    squad_members: list[str] | None = None,
    mood: str = "composed",
    time_of_day: str | None = None,
    costume: str | None = None,
) -> str:
    """Build the full positive prompt with quality tags, LoRA trigger, and character identities.

    Args:
        scene_tags: Scene/action Danbooru tags.
        couple: Whether to include the Commander.
        affection_level: 0-9, affects mood expression tags.
        context: Recent conversation text for outfit selection.
        squad_members: Optional list of squad member names to include in the scene.
        mood: Session mood (e.g. "tender", "playful") — adds a concise expression
            cue before the scene tags. Defaults to "composed". Unknown moods are
            ignored (no descriptor injected).
        time_of_day: One of morning/afternoon/evening/night, or None. When set,
            injects a short lighting/time cue before the scene tags.
        costume: Optional unlockable-wardrobe id (see OUTFIT_COSTUME_TAGS). When
            provided and recognized, its tag block REPLACES the keyword-matched
            outfit so the chosen skin actually drives the render. Unknown ids
            fall through to the existing keyword-context outfit logic.
    """
    parts = [QUALITY_TAGS, KLUKAI_LORA_TRIGGER]

    # Add affection-aware mood tags
    mood_tags = AFFECTION_MOOD_TAGS.get(affection_level, "")
    if mood_tags:
        parts.append(mood_tags)

    # Scene-aware descriptors — concise mood expression + time/lighting cue,
    # injected BEFORE the scene tags so they colour the moment without bloat.
    expression = MOOD_EXPRESSION_TAGS.get(mood or "")
    if expression:
        parts.append(expression)
    if time_of_day:
        lighting = TIME_OF_DAY_TAGS.get(time_of_day)
        if lighting:
            parts.append(lighting)

    # Context for outfit matching: use full context (conversation + scene tags)
    outfit_context = f"{context} {scene_tags}" if context else scene_tags
    # A selected (unlocked) wardrobe costume overrides the keyword-matched
    # outfit so the chosen skin actually changes the image; otherwise fall back
    # to the existing keyword-context outfit selection.
    if costume and costume in OUTFIT_COSTUME_TAGS:
        klukai_outfit = OUTFIT_COSTUME_TAGS[costume]
    else:
        klukai_outfit = _select_outfit(outfit_context, OUTFIT_MAP, KLUKAI_DEFAULT_OUTFIT)

    if couple:
        commander_outfit = _select_outfit(outfit_context, COMMANDER_OUTFIT_MAP, COMMANDER_DEFAULT_OUTFIT)
        parts.append(COUPLE_TAGS)
        parts.append(f"{COMMANDER_IDENTITY}, {commander_outfit}")
        parts.append(KLUKAI_IDENTITY)
    else:
        parts.append(KLUKAI_IDENTITY)
    parts.append(klukai_outfit)

    # Add squad members if specified
    if squad_members:
        for name in squad_members[:3]:  # Max 3 to avoid overcrowding
            if name in SQUAD_KEYWORDS:
                parts.append(SQUAD_KEYWORDS[name])
        total_girls = 1 + len([n for n in squad_members if n in SQUAD_KEYWORDS])
        if total_girls > 1:
            parts.append(f"{total_girls}girls, multiple girls")

    parts.append(scene_tags)
    return ", ".join(parts)


async def check_comfyui_ready() -> bool:
    """Check if ComfyUI is reachable and has the queue clear."""
    try:
        client = _get_http()
        r = await client.get(f"{COMFYUI_URL}/queue", timeout=5.0)
        if r.status_code != 200:
            return False
        data = r.json()
        running = len(data.get("queue_running", []))
        pending = len(data.get("queue_pending", []))
        if running > 0 or pending > 0:
            logger.info("ComfyUI busy: %d running, %d pending", running, pending)
            return False
        return True
    except Exception:
        return False


async def _interrupt_comfyui() -> None:
    """Interrupt the current ComfyUI generation."""
    try:
        client = _get_http()
        await client.post(f"{COMFYUI_URL}/interrupt", timeout=5.0)
        logger.info("ComfyUI generation interrupted")
        await asyncio.sleep(1)  # Let it settle
    except Exception as e:
        logger.warning("ComfyUI interrupt failed: %s", e)


async def _free_comfyui_vram() -> None:
    """Unload ComfyUI models from VRAM after generation to free space for LM Studio."""
    try:
        await asyncio.sleep(2)  # Let ComfyUI finish post-processing before unloading
        client = _get_http()
        await client.post(
            f"{COMFYUI_URL}/free",
            json={"unload_models": True, "free_memory": True},
            timeout=5.0,
        )
        logger.info("ComfyUI VRAM freed after image generation")
    except Exception as e:
        logger.warning("ComfyUI VRAM free failed: %s", e)


async def generate_image(
    prompt: str,
    width: int = 832,
    height: int = 1216,
    retry: bool = True,
) -> bytes | None:
    """Generate an image via ComfyUI. Semaphore ensures one at a time."""
    async with _image_gen_lock:
        return await _generate_image_inner(prompt, width, height, retry)


async def _generate_image_inner(
    prompt: str, width: int, height: int, retry: bool,
) -> bytes | None:
    try:
        result = await _try_generate(prompt, width, height)
        if result is None and retry:
            logger.info("Image generation retry — interrupting stale job and retrying")
            await _interrupt_comfyui()
            result = await _try_generate(prompt, width, height)
        return result
    finally:
        # Always free VRAM after gen so LM Studio can reclaim it
        await _free_comfyui_vram()


async def _try_generate(prompt: str, width: int, height: int) -> bytes | None:
    """Single attempt at image generation."""
    workflow = json.loads(json.dumps(WORKFLOW_TEMPLATE))

    workflow["6"]["inputs"]["text"] = prompt
    workflow["5"]["inputs"]["width"] = width
    workflow["5"]["inputs"]["height"] = height
    workflow["3"]["inputs"]["seed"] = int(uuid.uuid4().int % (2**32))

    try:
        client = _get_http()
        r = await client.post(
            f"{COMFYUI_URL}/prompt",
            json={"prompt": workflow},
        )
        if r.status_code != 200:
            logger.error("ComfyUI queue failed: %s", r.text[:200])
            return None

        prompt_id = r.json().get("prompt_id")
        if not prompt_id:
            return None

        # Poll for completion (up to 300s — first gen after model load can be slow)
        for _ in range(300):
            await asyncio.sleep(1)
            r = await client.get(f"{COMFYUI_URL}/history/{prompt_id}")
            if r.status_code == 200:
                history = r.json()
                if prompt_id in history:
                    outputs = history[prompt_id].get("outputs", {})
                    for output in outputs.values():
                        images = output.get("images", [])
                        if images:
                            img = images[0]
                            r2 = await client.get(
                                f"{COMFYUI_URL}/view",
                                params={
                                    "filename": img["filename"],
                                    "subfolder": img.get("subfolder", ""),
                                    "type": img.get("type", "output"),
                                },
                            )
                            if r2.status_code == 200:
                                logger.info("Image generated: %s (%d bytes)", img["filename"], len(r2.content))
                                return r2.content
                    return None

        logger.warning("Image generation timed out after 300s")
        return None
    except Exception as e:
        logger.error("Image generation failed: %s", e)
        return None
