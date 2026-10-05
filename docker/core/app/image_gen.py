"""Image generation via ComfyUI with NoobAI-XL (Illustrious) for anime-realistic scenes."""

from __future__ import annotations

import asyncio
import json
import logging
import hashlib
import os
import re
import uuid
from collections.abc import Awaitable, Callable

import httpx

from app.gpu_lease import (
    GPULease,
    GPULeaseError,
    gpu_lease,
    gpu_lease_auth_headers,
)

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
    IMG2IMG_TEMPLATE,
    INPAINT_NODES,
    KLUKAI_DEFAULT_OUTFIT,
    KLUKAI_IDENTITY,
    KLUKAI_LORA,
    KLUKAI_LORA_TRIGGER,
    LANDSCAPE_KEYWORDS,
    MISSION_SCENE_TAGS,
    MOOD_EXPRESSION_TAGS,
    NEGATIVE_TAGS,
    OUTFIT_MAP,
    INTIMATE_MIN_LEVEL,
    INTIMATE_OUTFIT_KEYS,
    QUALITY_TAGS,
    SCENE_OUTFIT_KEYWORDS,
    SFW_NEGATIVE_TAGS,
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
    "IMG2IMG_TEMPLATE",
    "INPAINT_NODES",
    "KLUKAI_DEFAULT_OUTFIT",
    "KLUKAI_IDENTITY",
    "KLUKAI_LORA",
    "KLUKAI_LORA_TRIGGER",
    "LANDSCAPE_KEYWORDS",
    "MISSION_SCENE_TAGS",
    "MOOD_EXPRESSION_TAGS",
    "NEGATIVE_TAGS",
    "OUTFIT_MAP",
    "INTIMATE_MIN_LEVEL",
    "INTIMATE_OUTFIT_KEYS",
    "QUALITY_TAGS",
    "SCENE_OUTFIT_KEYWORDS",
    "SFW_NEGATIVE_TAGS",
    "SITUATION_KEYWORDS",
    "SQUAD_KEYWORDS",
    "TIME_OF_DAY_TAGS",
    "WORKFLOW_TEMPLATE",
    "build_mission_prompt",
    "build_prompt",
    "check_comfyui_ready",
    "detect_squad_members",
    "free_comfyui_vram",
    "generate_image",
    "generate_img2img",
    "is_couple_scene",
    "is_landscape",
    "is_outfit_unlocked",
    "needs_image",
]

logger = logging.getLogger(__name__)

COMFYUI_URL = os.environ.get(
    "COMFYUI_URL", "http://100.107.121.5:1234/api/v1/comfy"
).rstrip("/")


_http: httpx.AsyncClient | None = None
_image_gen_lock = asyncio.Semaphore(
    1
)  # Only one image gen at a time — prevents GPU overload

# The gateway lease is fixed at 600 seconds and its clock starts while the
# gateway is still quiescing llama.cpp/native vLLM.  Cap Comfy work at eight
# minutes, leaving two minutes for acquisition drain time, interrupt/model
# cleanup, authenticated release, and scheduler jitter.
_IMAGE_LEASE_WORK_SECONDS = 8 * 60


def _get_http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        # Both bearer auth and the lease capability are sent per request.
        # Never let ambient proxies or redirects move them off the literal
        # Tailscale gateway route.
        _http = httpx.AsyncClient(
            timeout=180.0,
            trust_env=False,
            follow_redirects=False,
        )
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
    parts.append("high ponytail, tactical gear, body armor, combat vest, rifle, intense")

    # Add injury tags to Klukai if she's hurt
    if injuries and "klukai_injured" in injuries:
        parts.append(
            "bandaged arm, torn sleeve, blood stains, determined expression, still commanding"
        )

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

    lower = text.lower()
    return any(
        re.search(r"\b" + re.escape(kw) + r"\b", lower) for kw in COUPLE_KEYWORDS
    )


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

    Delegates to the wardrobe catalog (app/wardrobe.py). Legacy ids map
    forward; an unknown id is locked (fail-closed).
    """
    from . import wardrobe

    return wardrobe.is_unlocked(costume, affection_level)


def _explicit_scene(text: str) -> bool:
    """An explicit bath/bed/lingerie scene (whole words), which outranks today's outfit."""
    lower = text.lower()
    return any(re.search(rf"\b{kw}\b", lower) for kw in SCENE_OUTFIT_KEYWORDS)


def _outfit_map_for(affection_level: int) -> dict[str, str]:
    """OUTFIT_MAP minus the intimate outfits below the intimacy gate."""
    if affection_level >= INTIMATE_MIN_LEVEL:
        return OUTFIT_MAP
    return {k: v for k, v in OUTFIT_MAP.items() if k not in INTIMATE_OUTFIT_KEYS}


def build_prompt(
    scene_tags: str,
    couple: bool = False,
    affection_level: int = 0,
    context: str = "",
    squad_members: list[str] | None = None,
    mood: str = "composed",
    time_of_day: str | None = None,
    costume: str | None = None,
    request: str | None = None,
    affection_tags: bool = True,
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
        costume: Optional wardrobe id (app/wardrobe.py catalog) — what she is
            wearing. Its tag block replaces the keyword-matched outfit, unless
            the scene is explicitly a bath/bed/lingerie one. Unknown ids fall
            through to the keyword-context outfit logic.
        request: His own words for this image, when known. Only these (never
            her replies in ``context``) can make it an explicit bath/bed scene.
        affection_tags: Inject the affection-level expression/setting tags.
            Live Portrait turns this off: its frames set the expression
            themselves and need a neutral, setting-free base.
    """
    parts = [QUALITY_TAGS, KLUKAI_LORA_TRIGGER]

    # Add affection-aware mood tags
    mood_tags = AFFECTION_MOOD_TAGS.get(affection_level, "") if affection_tags else ""
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
    # What she is wearing today overrides the keyword-matched outfit, except
    # for an explicit bath/bed scene; otherwise fall back to keyword selection.
    from . import wardrobe

    costume_tags = wardrobe.image_tags(costume) if costume else None
    explicit = affection_level >= INTIMATE_MIN_LEVEL and _explicit_scene(
        request if request is not None else outfit_context
    )
    if costume_tags and not explicit:
        klukai_outfit = costume_tags
    else:
        klukai_outfit = _select_outfit(
            outfit_context, _outfit_map_for(affection_level), KLUKAI_DEFAULT_OUTFIT
        )

    if couple:
        commander_outfit = _select_outfit(
            outfit_context, COMMANDER_OUTFIT_MAP, COMMANDER_DEFAULT_OUTFIT
        )
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
    """Check ComfyUI's queue only while holding its exclusive GPU lease.

    This remains as a compatibility helper for callers that explicitly need a
    queue probe.  Normal image generation performs its own work under one
    continuous lease and does not use a separate preflight lease.
    """
    from .llm_router import get_lm_gate

    async with _image_gen_lock:
        async with get_lm_gate():
            try:
                async with gpu_lease("comfyui") as lease:
                    client = _get_http()
                    response = await client.get(
                        f"{COMFYUI_URL}/queue",
                        headers=gpu_lease_auth_headers(lease),
                        timeout=5.0,
                    )
                    if response.status_code != 200:
                        return False
                    data = response.json()
                    running = len(data.get("queue_running", []))
                    pending = len(data.get("queue_pending", []))
                    if running > 0 or pending > 0:
                        logger.info(
                            "ComfyUI busy: %d running, %d pending", running, pending
                        )
                        return False
                    return True
            except (GPULeaseError, httpx.HTTPError, TypeError, ValueError):
                return False


async def _interrupt_comfyui(lease: GPULease) -> bool:
    """Interrupt the current ComfyUI generation."""
    try:
        client = _get_http()
        response = await client.post(
            f"{COMFYUI_URL}/interrupt",
            headers=gpu_lease_auth_headers(lease),
            timeout=5.0,
        )
        if response.status_code != 200:
            logger.warning("ComfyUI interrupt returned HTTP %s", response.status_code)
            return False
        logger.info("ComfyUI generation interrupted")
        await asyncio.sleep(1)  # Let it settle
        return True
    except Exception as e:
        logger.warning("ComfyUI interrupt failed: %s", e)
        return False


async def _free_comfyui_vram(lease: GPULease) -> bool:
    """Unload ComfyUI weights, retrying before the gateway lease is released."""
    await asyncio.sleep(2)  # Let ComfyUI finish post-processing before unloading
    client = _get_http()
    for attempt in range(1, 4):
        try:
            response = await client.post(
                f"{COMFYUI_URL}/free",
                headers=gpu_lease_auth_headers(lease),
                json={"unload_models": True, "free_memory": True},
                timeout=5.0,
            )
            if response.status_code == 200:
                logger.info("ComfyUI VRAM freed after image generation")
                return True
            logger.warning(
                "ComfyUI VRAM free returned HTTP %s (attempt %d/3)",
                response.status_code,
                attempt,
            )
        except Exception as exc:
            logger.warning(
                "ComfyUI VRAM free failed (%s, attempt %d/3)",
                type(exc).__name__,
                attempt,
            )
        if attempt < 3:
            await asyncio.sleep(1)
    logger.error("ComfyUI VRAM could not be confirmed free after three attempts")
    return False


async def free_comfyui_vram() -> bool:
    """Safely release stale Comfy weights under an authenticated GPU lease."""
    from .llm_router import get_lm_gate

    async with _image_gen_lock:
        async with get_lm_gate():
            try:
                async with gpu_lease("comfyui") as lease:
                    return await _free_comfyui_vram(lease)
            except GPULeaseError as exc:
                logger.warning("ComfyUI cleanup GPU handoff refused: %s", exc)
                return False


def negative_prompt(sfw: bool = False) -> str:
    """The negative prompt; ``sfw`` adds the below-intimacy-gate backstop."""
    return f"{NEGATIVE_TAGS}, {SFW_NEGATIVE_TAGS}" if sfw else NEGATIVE_TAGS


async def generate_image(
    prompt: str,
    width: int = 832,
    height: int = 1216,
    retry: bool = True,
    *,
    sfw: bool = False,
    seed: int | None = None,
) -> bytes | None:
    """Generate under an exclusive gateway lease, one image at a time.

    ``sfw=True`` (wardrobe-driven renders below the intimacy gate) adds
    SFW_NEGATIVE_TAGS to the negative prompt. ``seed`` pins the sampler seed
    (Live Portrait bases); by default every render gets a fresh one.

    The shared LM gate drains Klukai's own local-LLM calls.  The authenticated
    gateway lease then drains external inference, unloads llama.cpp, and keeps
    all model loads blocked for the complete ComfyUI operation.
    """
    return await _leased(
        lambda lease: _generate_image_inner(
            prompt, width, height, retry, lease, sfw=sfw, seed=seed
        )
    )


async def generate_img2img(
    source_png: bytes,
    prompt: str,
    *,
    denoise: float,
    seed: int,
    sfw: bool = False,
    mask_png: bytes | None = None,
) -> bytes | None:
    """img2img from ``source_png`` under the same lease path as generate_image.

    The source is uploaded through the gateway facade, encoded, and re-sampled
    with ``denoise`` (0 = identical, 1 = ignore the source) on the same
    checkpoint + LoRA chain. With ``mask_png`` (white = repaint) only the
    masked region changes and everything else is the source pixel for pixel
    (the Live Portrait expression frames).
    """
    return await _leased(
        lambda lease: _img2img_inner(
            source_png, prompt, denoise, seed, sfw, lease, mask_png=mask_png
        )
    )


async def _leased(render: Callable[[GPULease], Awaitable[bytes | None]]) -> bytes | None:
    """Run ``render`` under the image lock, LM gate and one ComfyUI GPU lease."""
    async with _image_gen_lock:
        # Lazy import avoids coupling image prompt helpers to the LLM router at
        # module import time.
        from .llm_router import get_lm_gate

        async with get_lm_gate():
            try:
                async with gpu_lease("comfyui") as lease:
                    try:
                        async with asyncio.timeout(_IMAGE_LEASE_WORK_SECONDS):
                            return await render(lease)
                    except TimeoutError:
                        logger.error(
                            "Image generation exceeded the bounded GPU lease window"
                        )
                        # A timed-out Comfy job may still be executing.  Stop it
                        # and release its weights before the lease can be returned.
                        interrupted = await _interrupt_comfyui(lease)
                        cleaned = await _free_comfyui_vram(lease)
                        if not interrupted or not cleaned:
                            # The gateway release endpoint is the final cleanup
                            # authority and must retain a fail-closed marker if
                            # it cannot prove quiescence.  Still reject the job
                            # locally rather than treating failed cleanup as
                            # success.
                            raise GPULeaseError(
                                "ComfyUI timeout cleanup could not be confirmed"
                            ) from None
                        return None
            except GPULeaseError as exc:
                # The exception is intentionally credential-free.
                logger.warning("Image GPU handoff refused: %s", exc)
                return None


async def _generate_image_inner(
    prompt: str,
    width: int,
    height: int,
    retry: bool,
    lease: GPULease,
    sfw: bool = False,
    seed: int | None = None,
) -> bytes | None:
    try:
        result = await _try_generate(prompt, width, height, lease, sfw=sfw, seed=seed)
        if result is None and retry:
            logger.info("Image generation retry — interrupting stale job and retrying")
            if not await _interrupt_comfyui(lease):
                raise GPULeaseError("ComfyUI retry interrupt could not be confirmed")
            result = await _try_generate(prompt, width, height, lease, sfw=sfw, seed=seed)
        return result
    finally:
        # Always free VRAM after gen so LM Studio can reclaim it
        if not await _free_comfyui_vram(lease):
            raise GPULeaseError("ComfyUI VRAM cleanup could not be confirmed")


async def _img2img_inner(
    source_png: bytes,
    prompt: str,
    denoise: float,
    seed: int,
    sfw: bool,
    lease: GPULease,
    mask_png: bytes | None = None,
) -> bytes | None:
    """Upload the source (and mask), then one img2img attempt; VRAM is always freed."""
    try:
        name = await _upload_image(source_png, lease)
        if name is None:
            return None
        workflow = json.loads(json.dumps(IMG2IMG_TEMPLATE))
        if mask_png is not None:
            mask_name = await _upload_image(mask_png, lease)
            if mask_name is None:
                return None
            workflow.update(json.loads(json.dumps(INPAINT_NODES)))
            workflow["13"]["inputs"]["image"] = mask_name
            workflow["3"]["inputs"]["latent_image"] = ["14", 0]
            workflow["3"]["inputs"]["model"] = ["16", 0]
            workflow["9"]["inputs"]["images"] = ["15", 0]
        workflow["6"]["inputs"]["text"] = prompt
        workflow["7"]["inputs"]["text"] = negative_prompt(sfw)
        workflow["11"]["inputs"]["image"] = name
        workflow["3"]["inputs"]["seed"] = int(seed) % (2**32)
        workflow["3"]["inputs"]["denoise"] = max(0.0, min(1.0, float(denoise)))
        return await _run_workflow(workflow, lease)
    finally:
        if not await _free_comfyui_vram(lease):
            raise GPULeaseError("ComfyUI VRAM cleanup could not be confirmed")


async def _upload_image(png: bytes, lease: GPULease) -> str | None:
    """Upload a source image to ComfyUI's input folder; returns its LoadImage name."""
    try:
        client = _get_http()
        r = await client.post(
            f"{COMFYUI_URL}/upload/image",
            headers=gpu_lease_auth_headers(lease),
            # Content-addressed name + overwrite: re-uploading the same base or
            # mask reuses one input file instead of piling up copies.
            files={"image": (f"klukai_src_{hashlib.sha256(png).hexdigest()[:16]}.png", png, "image/png")},
            data={"type": "input", "overwrite": "true"},
        )
        if r.status_code != 200:
            logger.error("ComfyUI upload failed: HTTP %s", r.status_code)
            return None
        data = r.json()
        name = str(data.get("name") or "")
        sub = str(data.get("subfolder") or "")
        return (f"{sub}/{name}" if sub else name) or None
    except Exception as e:
        logger.error("ComfyUI upload failed: %s", e)
        return None


async def _try_generate(
    prompt: str,
    width: int,
    height: int,
    lease: GPULease,
    sfw: bool = False,
    seed: int | None = None,
) -> bytes | None:
    """Single attempt at image generation."""
    workflow = json.loads(json.dumps(WORKFLOW_TEMPLATE))

    workflow["6"]["inputs"]["text"] = prompt
    workflow["7"]["inputs"]["text"] = negative_prompt(sfw)
    workflow["5"]["inputs"]["width"] = width
    workflow["5"]["inputs"]["height"] = height
    workflow["3"]["inputs"]["seed"] = (
        int(seed) % (2**32) if seed is not None else int(uuid.uuid4().int % (2**32))
    )
    return await _run_workflow(workflow, lease)


async def _run_workflow(workflow: dict, lease: GPULease) -> bytes | None:
    """Queue ``workflow``, poll its history, and fetch the first output image."""
    try:
        client = _get_http()
        r = await client.post(
            f"{COMFYUI_URL}/prompt",
            headers=gpu_lease_auth_headers(lease),
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
            r = await client.get(
                f"{COMFYUI_URL}/history/{prompt_id}",
                headers=gpu_lease_auth_headers(lease),
            )
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
                                headers=gpu_lease_auth_headers(lease),
                                params={
                                    "filename": img["filename"],
                                    "subfolder": img.get("subfolder", ""),
                                    "type": img.get("type", "output"),
                                },
                            )
                            if r2.status_code == 200:
                                logger.info(
                                    "Image generated: %s (%d bytes)",
                                    img["filename"],
                                    len(r2.content),
                                )
                                return r2.content
                    return None

        logger.warning("Image generation timed out after 300s")
        return None
    except Exception as e:
        logger.error("Image generation failed: %s", e)
        return None
