"""Image generation via ComfyUI with NoobAI-XL (Illustrious) for anime-realistic scenes."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

from app.gpu_lease import (
    GPU_LEASE_ACQUIRE_READ_TIMEOUT_SECONDS,
    GPU_LEASE_TTL_SECONDS,
    GPULease,
    GPULeaseError,
    gpu_lease,
    gpu_lease_auth_headers,
)

# File-size hygiene (same rubric as the constants split): the pure prompt
# layer lives in image_gen_prompts.py and the ComfyUI client — everything
# handed an already-held lease — in image_gen_comfy.py.  This module keeps the
# lease policy, the public API and the render bodies, and re-exports the public
# names so `from app.image_gen import build_prompt` keeps working.  The Comfy
# helpers are bound into THIS namespace because _leased, free_comfyui_vram,
# check_comfyui_ready and the render bodies look them up here (tests patch
# `app.image_gen._free_comfyui_vram` etc. for those callers).
from app.image_gen_comfy import (
    COMFYUI_URL,
    DEFAULT_SAMPLING,
    InpaintBranch,
    InpaintPlan,
    Sampling,
    _free_comfyui_vram,
    _get_http,
    _inpaint_set_inner,
    _interrupt_comfyui,
    _run_workflow,
    _txt2img_workflow,
    _upload_image,
)
from app.image_gen_prompts import (
    build_mission_prompt,
    build_prompt,
    detect_squad_members,
    is_couple_scene,
    is_landscape,
    is_outfit_unlocked,
    needs_image,
    negative_prompt,
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
    "generate_inpaint_set",
    "InpaintBranch",
    "Sampling",
    "is_couple_scene",
    "is_landscape",
    "is_outfit_unlocked",
    "needs_image",
    "negative_prompt",
]

logger = logging.getLogger(__name__)


_image_gen_lock = asyncio.Semaphore(
    1
)  # Only one image gen at a time — prevents GPU overload

# The gateway lease is fixed at 600 seconds and its clock starts while the
# gateway is still quiescing llama.cpp/native vLLM — i.e. acquisition itself
# can eat up to the full acquire read deadline (150s) before we see a token.
# Budget the Comfy work honestly: TTL minus that worst-case acquire, minus
# 30s for interrupt/model cleanup, the authenticated release and scheduler
# jitter.  The old 8-minute cap assumed two minutes of headroom; a slow
# acquire plus a long render had the post-render /free hit an expired lease,
# so a finished image was thrown away.
_IMAGE_LEASE_CLEANUP_SECONDS = 30
_IMAGE_LEASE_WORK_SECONDS = int(
    GPU_LEASE_TTL_SECONDS
    - GPU_LEASE_ACQUIRE_READ_TIMEOUT_SECONDS
    - _IMAGE_LEASE_CLEANUP_SECONDS
)


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


async def generate_image(
    prompt: str,
    width: int = 832,
    height: int = 1216,
    retry: bool = True,
    *,
    sfw: bool = False,
    seed: int | None = None,
    negative_extra: str | None = None,
) -> bytes | None:
    """Generate under an exclusive gateway lease, one image at a time.

    ``sfw=True`` (wardrobe-driven renders below the intimacy gate) adds
    SFW_NEGATIVE_TAGS to the negative prompt; ``negative_extra`` appends a
    caller's own negatives (Live Portrait's palette guard). ``seed`` pins the
    sampler seed (Live Portrait bases); by default every render gets a fresh one.

    The shared LM gate drains Klukai's own local-LLM calls.  The authenticated
    gateway lease then drains external inference, unloads llama.cpp, and keeps
    all model loads blocked for the complete ComfyUI operation.
    """
    return await _leased(
        lambda lease: _generate_image_inner(
            prompt, width, height, retry, lease, sfw=sfw, seed=seed,
            negative_extra=negative_extra,
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
    negative_extra: str | None = None,
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
            source_png, prompt, denoise, seed, sfw, lease, mask_png=mask_png,
            negative_extra=negative_extra,
        )
    )


_T = TypeVar("_T")


async def _leased(render: Callable[[GPULease], Awaitable[_T]]) -> _T | None:
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
    negative_extra: str | None = None,
) -> bytes | None:
    try:
        result = await _try_generate(
            prompt, width, height, lease, sfw=sfw, seed=seed, negative_extra=negative_extra
        )
        if result is None and retry:
            logger.info("Image generation retry — interrupting stale job and retrying")
            if not await _interrupt_comfyui(lease):
                raise GPULeaseError("ComfyUI retry interrupt could not be confirmed")
            result = await _try_generate(
                prompt, width, height, lease, sfw=sfw, seed=seed, negative_extra=negative_extra
            )
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
    negative_extra: str | None = None,
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
        workflow["7"]["inputs"]["text"] = negative_prompt(sfw, negative_extra)
        workflow["11"]["inputs"]["image"] = name
        workflow["3"]["inputs"]["seed"] = int(seed) % (2**32)
        workflow["3"]["inputs"]["denoise"] = max(0.0, min(1.0, float(denoise)))
        return await _run_workflow(workflow, lease)
    finally:
        if not await _free_comfyui_vram(lease):
            raise GPULeaseError("ComfyUI VRAM cleanup could not be confirmed")


async def _try_generate(
    prompt: str,
    width: int,
    height: int,
    lease: GPULease,
    sfw: bool = False,
    seed: int | None = None,
    negative_extra: str | None = None,
) -> bytes | None:
    """Single attempt at image generation."""
    workflow = _txt2img_workflow(prompt, width, height, sfw, seed, negative_extra)
    return await _run_workflow(workflow, lease)


async def generate_inpaint_set(
    *,
    base_prompt: str,
    base_png: bytes | None,
    plan: InpaintPlan,
    seed: int,
    width: int = 832,
    height: int = 1216,
    sfw: bool = False,
    negative_extra: str | None = None,
    base_sampling: Sampling = DEFAULT_SAMPLING,
    inpaint_sampling: Sampling = DEFAULT_SAMPLING,
    on_image: Callable[[str, bytes], Awaitable[None]] | None = None,
    accept: Callable[[str, bytes], bool] | None = None,
    retries: int = 2,
    retry_denoise_step: float = 0.0,
    fallback: Callable[[str, list[bytes]], bytes | None] | None = None,
) -> dict[str, bytes]:
    """A base and its inpaint branches under ONE lease and ONE model load.

    Renders the base (unless ``base_png`` is given), lets ``plan(base)`` cut
    the shared source and masks, then runs every branch as one ComfyUI graph.
    ``on_image(name, png)`` fires as each image lands ("base" first), so the
    caller can show it before the rest are done. A branch ``accept`` rejects
    is re-run with the next seed, up to ``retries`` times, each time with its
    denoise raised by ``retry_denoise_step``. If a branch is still rejected
    after the last retry, ``fallback(name, candidates)`` may pick one of the
    rejected renders (or return None to leave the branch missing). VRAM is
    freed once, at the end. Returns the accepted images ("base" only if
    rendered here); empty if the lease was refused or the base failed.
    """
    result = await _leased(lambda lease: _inpaint_set_inner(
        lease, base_prompt=base_prompt, base_png=base_png, plan=plan, seed=seed,
        width=width, height=height, sfw=sfw, negative_extra=negative_extra,
        base_sampling=base_sampling, inpaint_sampling=inpaint_sampling,
        on_image=on_image, accept=accept, retries=retries,
        retry_denoise_step=retry_denoise_step, fallback=fallback,
    ))
    return result or {}
