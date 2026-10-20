"""Image generation: prompts for every renderer, and the legacy ComfyUI path.

ComfyUI with NoobAI-XL (Illustrious) on Dominus runs only when
``KLUKAI_IMAGE_PROVIDER=comfyui`` is set explicitly; every other provider is
dispatched before any GPU gate, lease or Dominus request.
"""

from __future__ import annotations

from typing import Any, Unpack


import asyncio
import json
import logging
import os
import uuid as uuid
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
from .shared_http import client as http_client, close as close_http_client

from app.gpu_lease import (
    GPU_LEASE_ACQUIRE_READ_TIMEOUT_SECONDS,
    GPU_LEASE_TTL_SECONDS,
    GPULease,
    GPULeaseError,
    gpu_lease,
    confirm_gpu_cleanup,
)

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
    IMG2IMG_TEMPLATE as IMG2IMG_TEMPLATE,
    INPAINT_NODES as INPAINT_NODES,
    KLUKAI_DEFAULT_OUTFIT as KLUKAI_DEFAULT_OUTFIT,
    KLUKAI_IDENTITY as KLUKAI_IDENTITY,
    KLUKAI_LORA as KLUKAI_LORA,
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
    WORKFLOW_TEMPLATE as WORKFLOW_TEMPLATE,
)

# Public re-exports — keep linter happy AND preserve `from app.image_gen import X` paths.
__all__ = (
    "AFFECTION_MOOD_TAGS COMMANDER_DEFAULT_OUTFIT COMMANDER_IDENTITY "
    "COMMANDER_OUTFIT_MAP COUPLE_KEYWORDS COUPLE_TAGS IMAGE_KEYWORDS "
    "IMG2IMG_TEMPLATE INPAINT_NODES KLUKAI_DEFAULT_OUTFIT KLUKAI_IDENTITY "
    "KLUKAI_LORA KLUKAI_LORA_TRIGGER LANDSCAPE_KEYWORDS MISSION_SCENE_TAGS "
    "MOOD_EXPRESSION_TAGS NEGATIVE_TAGS OUTFIT_MAP INTIMATE_MIN_LEVEL "
    "INTIMATE_OUTFIT_KEYS QUALITY_TAGS SCENE_OUTFIT_KEYWORDS "
    "SFW_NEGATIVE_TAGS SITUATION_KEYWORDS SQUAD_KEYWORDS TIME_OF_DAY_TAGS "
    "WORKFLOW_TEMPLATE build_mission_prompt build_prompt check_comfyui_ready "
    "detect_squad_members free_comfyui_vram generate_image generate_img2img "
    "generate_inpaint_set InpaintBranch Sampling is_couple_scene is_landscape "
    "is_outfit_unlocked needs_image"
).split()


from .image_comfy import ComfyTransport
from .image_leases import run_leased
from .image_rendering import render_with_cleanup
from .image_inpaint import InpaintRenderer, InpaintSetOptions
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
from .image_workflows import (
    Sampling as Sampling,
    InpaintBranch as InpaintBranch,
    _txt2img_workflow as _txt2img_workflow,
    _inpaint_graph as _inpaint_graph,
    DEFAULT_SAMPLING as DEFAULT_SAMPLING,
)

logger = logging.getLogger(__name__)

COMFYUI_URL = os.environ.get(
    "COMFYUI_URL", "http://100.107.121.5:1234/api/v1/comfy"
).rstrip("/")


_http: httpx.AsyncClient | None = None
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


def _get_http() -> httpx.AsyncClient:
    global _http
    _http = http_client(_http, timeout=180.0, trust_env=False, follow_redirects=False)
    return _http


async def close() -> None:
    global _http
    client, _http = _http, None
    await close_http_client(client)


async def check_comfyui_ready() -> bool:
    """Check ComfyUI's queue only while holding its exclusive GPU lease.

    This remains as a compatibility helper for callers that explicitly need a
    queue probe.  Normal image generation performs its own work under one
    continuous lease and does not use a separate preflight lease.
    """
    from .image_provider import selected_provider

    provider = selected_provider()
    if provider == "agy":
        from .agy_image_provider import agy_images_ready

        return await agy_images_ready()
    if provider != "comfyui":
        return False
    from .llm_router import get_lm_gate

    async with _image_gen_lock:
        async with get_lm_gate():
            try:
                async with gpu_lease("comfyui") as lease:
                    return await _transport().ready(lease)
            except (GPULeaseError, httpx.HTTPError, TypeError, ValueError):
                return False


async def _interrupt_comfyui(lease: GPULease) -> bool:
    return await confirm_gpu_cleanup(_transport().interrupt_comfyui(lease))


async def _free_comfyui_vram(lease: GPULease, settle_s: float = 2.0) -> bool:
    return await confirm_gpu_cleanup(_transport().free_comfyui_vram(lease, settle_s))


async def free_comfyui_vram() -> bool:
    """Safely release stale Comfy weights under an authenticated GPU lease."""
    from .image_provider import selected_provider

    if selected_provider() != "comfyui":
        return True
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
    user_id: str | None = None,
    include_commander: bool = False,
) -> bytes | None:
    """Generate through the configured scene provider (default: the account
    worker; ComfyUI on Dominus only with ``KLUKAI_IMAGE_PROVIDER=comfyui``).

    ``sfw=True`` (wardrobe-driven renders below the intimacy gate) adds
    SFW_NEGATIVE_TAGS to the negative prompt; ``negative_extra`` appends a
    caller's own negatives (Live Portrait's palette guard). ``seed`` pins the
    sampler seed (Live Portrait bases); by default every render gets a fresh one.

    Cloud scene requests use a separate bounded, reference-based renderer;
    they do not acquire the local LM gate or GPU lease. ``retry`` and ``seed``
    apply only to ComfyUI. Cloud allowance failures propagate to batch callers.

    For local images, the shared LM gate drains Klukai's own local-LLM calls. The authenticated
    gateway lease then drains external inference, unloads llama.cpp, and keeps
    all model loads blocked for the complete ComfyUI operation.
    """
    # Dispatch before any local semaphore, LM gate or GPU lease. Cloud failure
    # must not trigger a local retry or wake a sleeping/gaming Dominus.
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
            "nudity": image_provider.nudity_allowed(
                prompt, intimate=not sfw, include_commander=include_commander
            ),
        }
        result = await submit_agy_image(description, **request)
        if (
            result.get("status") == "busy"
            and not account_images.in_background_render()
            and await account_images.wait_for_background_render()
        ):
            # A background portrait/album render held the worker's one slot;
            # "busy" proves this was never accepted, so it goes next, once.
            result = await submit_agy_image(description, **request)
        return result.get("image") if result.get("status") == "completed" else None
    if provider == "openai":
        return await image_provider.generate_cloud_image(
            prompt,
            width,
            height,
            sfw=sfw,
            negative_extra=negative_extra,
        )
    if provider != "comfyui":
        logger.warning("Unknown image provider; generation disabled")
        return None
    return await _leased(
        lambda lease: _generate_image_inner(
            prompt,
            width,
            height,
            retry,
            lease,
            sfw=sfw,
            seed=seed,
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
            source_png,
            prompt,
            denoise,
            seed,
            sfw,
            lease,
            mask_png=mask_png,
            negative_extra=negative_extra,
        )
    )


_T = TypeVar("_T")


async def _leased(render: Callable[[GPULease], Awaitable[_T]]) -> _T | None:
    from .image_provider import selected_provider

    if selected_provider() != "comfyui":
        return None
    from .llm_router import get_lm_gate

    return await run_leased(
        render,
        lock=_image_gen_lock,
        gate=get_lm_gate(),
        acquire=gpu_lease,
        timeout=_IMAGE_LEASE_WORK_SECONDS,
        interrupt=_interrupt_comfyui,
        free=_free_comfyui_vram,
    )


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
    return await render_with_cleanup(
        lambda: _try_generate(
            prompt,
            width,
            height,
            lease,
            sfw=sfw,
            seed=seed,
            negative_extra=negative_extra,
        ),
        retry=retry,
        lease=lease,
        interrupt=_interrupt_comfyui,
        free=_free_comfyui_vram,
    )


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


async def _upload_image(png: bytes, lease: GPULease) -> str | None:
    return await _transport().upload_image(png, lease)


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


# History polling: fast at first (a portrait frame is done in ~2s), then 1s.
_POLL_FAST_S = 0.25
_POLL_FAST_COUNT = 40


async def _queue_and_wait(
    workflow: dict[str, Any], lease: GPULease
) -> dict[str, Any] | None:
    return await _transport().queue_and_wait(workflow, lease)


async def _fetch_image(img: dict[str, Any], lease: GPULease) -> bytes | None:
    return await _transport().fetch_image(img, lease)


async def _run_workflow(workflow: dict[str, Any], lease: GPULease) -> bytes | None:
    return await _transport().run_workflow(workflow, lease)


async def _run_graph(workflow: dict[str, Any], lease: GPULease) -> dict[str, bytes]:
    return await _transport().run_graph(workflow, lease)


InpaintPlan = Callable[[bytes], Awaitable[tuple[bytes, list[InpaintBranch]]]]
_SET_SETTLE_S = 0.25


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
    result = await _leased(
        lambda lease: _inpaint_set_inner(
            lease,
            base_prompt=base_prompt,
            base_png=base_png,
            plan=plan,
            seed=seed,
            width=width,
            height=height,
            sfw=sfw,
            negative_extra=negative_extra,
            base_sampling=base_sampling,
            inpaint_sampling=inpaint_sampling,
            on_image=on_image,
            accept=accept,
            retries=retries,
            retry_denoise_step=retry_denoise_step,
            fallback=fallback,
        )
    )
    return result or {}


async def _inpaint_set_inner(
    lease: GPULease, **options: Unpack[InpaintSetOptions]
) -> dict[str, bytes]:
    renderer = InpaintRenderer(
        _upload_image, _run_workflow, _run_graph, _free_comfyui_vram
    )
    return await renderer.render(lease, **options)


def _transport() -> ComfyTransport:
    # Construct no client/state here: all adapters share the original client.
    return ComfyTransport(
        _get_http(), COMFYUI_URL, range(300), _POLL_FAST_S, _POLL_FAST_COUNT
    )
