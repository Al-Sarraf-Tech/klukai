"""The ComfyUI client behind app.image_gen: the work that runs under a held GPU lease.

app.image_gen owns the lease policy (the image lock, the shared LM gate, the
authenticated gateway lease and its work budget — ``_leased``) and the render
bodies it hands a lease to.  Everything here is given that already-held
``GPULease`` and talks to ComfyUI through the gateway facade: the HTTP client,
interrupt and VRAM free, source uploads, queue/poll/fetch, the txt2img workflow
and the one-lease inpaint sets (Live Portrait).  Nothing here acquires a lease;
a VRAM free that cannot be confirmed raises GPULeaseError so the lease is never
returned as if clean.
"""

from __future__ import annotations

import asyncio
import json
import logging
import hashlib
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

import httpx

from app.gpu_lease import GPULease, GPULeaseError, gpu_lease_auth_headers
from app.image_gen_constants import WORKFLOW_TEMPLATE
from app.image_gen_prompts import negative_prompt

logger = logging.getLogger(__name__)


COMFYUI_URL = os.environ.get(
    "COMFYUI_URL", "http://100.107.121.5:1234/api/v1/comfy"
).rstrip("/")


_http: httpx.AsyncClient | None = None


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


async def _free_comfyui_vram(lease: GPULease, settle_s: float = 2.0) -> bool:
    """Unload ComfyUI weights, retrying before the gateway lease is released.

    ``settle_s`` lets ComfyUI finish post-processing first; a caller that has
    already read the finished history (the inpaint set) can pass less.
    """
    await asyncio.sleep(settle_s)
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


def _txt2img_workflow(
    prompt: str,
    width: int,
    height: int,
    sfw: bool,
    seed: int | None,
    negative_extra: str | None,
    sampling: Sampling | None = None,
) -> dict:
    workflow = json.loads(json.dumps(WORKFLOW_TEMPLATE))
    workflow["6"]["inputs"]["text"] = prompt
    workflow["7"]["inputs"]["text"] = negative_prompt(sfw, negative_extra)
    workflow["5"]["inputs"]["width"] = width
    workflow["5"]["inputs"]["height"] = height
    workflow["3"]["inputs"]["seed"] = (
        int(seed) % (2**32) if seed is not None else int(uuid.uuid4().int % (2**32))
    )
    if sampling is not None:
        workflow["3"]["inputs"].update(
            sampler_name=sampling.sampler, scheduler=sampling.scheduler,
            steps=sampling.steps, cfg=sampling.cfg,
        )
    return workflow


# History polling: fast at first (a portrait frame is done in ~2s), then 1s.
_POLL_FAST_S = 0.25
_POLL_FAST_COUNT = 40


async def _queue_and_wait(workflow: dict, lease: GPULease) -> dict | None:
    """Queue ``workflow`` and wait for its history; returns its outputs or None."""
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

    # Poll for completion (first gen after a model load can be slow)
    for i in range(300):
        await asyncio.sleep(_POLL_FAST_S if i < _POLL_FAST_COUNT else 1)
        r = await client.get(
            f"{COMFYUI_URL}/history/{prompt_id}",
            headers=gpu_lease_auth_headers(lease),
        )
        if r.status_code == 200:
            history = r.json()
            if prompt_id in history:
                return dict(history[prompt_id].get("outputs", {}))

    logger.warning("Image generation timed out waiting for ComfyUI history")
    return None


async def _fetch_image(img: dict, lease: GPULease) -> bytes | None:
    r = await _get_http().get(
        f"{COMFYUI_URL}/view",
        headers=gpu_lease_auth_headers(lease),
        params={
            "filename": img["filename"],
            "subfolder": img.get("subfolder", ""),
            "type": img.get("type", "output"),
        },
    )
    if r.status_code != 200:
        return None
    logger.info("Image generated: %s (%d bytes)", img["filename"], len(r.content))
    return r.content


async def _run_workflow(workflow: dict, lease: GPULease) -> bytes | None:
    """Queue ``workflow``, poll its history, and fetch the first output image."""
    try:
        outputs = await _queue_and_wait(workflow, lease)
        for output in (outputs or {}).values():
            images = output.get("images", [])
            if images:
                return await _fetch_image(images[0], lease)
        return None
    except Exception as e:
        logger.error("Image generation failed: %s", e)
        return None


async def _run_graph(workflow: dict, lease: GPULease) -> dict[str, bytes]:
    """Queue a multi-output graph; returns {SaveImage node id: first image}."""
    try:
        outputs = await _queue_and_wait(workflow, lease)
        result: dict[str, bytes] = {}
        for node_id, output in (outputs or {}).items():
            images = output.get("images", [])
            if images:
                data = await _fetch_image(images[0], lease)
                if data:
                    result[str(node_id)] = data
        return result
    except Exception as e:
        logger.error("Image graph failed: %s", e)
        return {}


# ── One-lease inpaint sets (Live Portrait) ──────────────────────────────────


_TEMPLATE_KSAMPLER = WORKFLOW_TEMPLATE["3"]["inputs"]


@dataclass(frozen=True)
class Sampling:
    """KSampler settings; the defaults are WORKFLOW_TEMPLATE's (the model's own)."""

    sampler: str = _TEMPLATE_KSAMPLER["sampler_name"]
    scheduler: str = _TEMPLATE_KSAMPLER["scheduler"]
    steps: int = _TEMPLATE_KSAMPLER["steps"]
    cfg: float = _TEMPLATE_KSAMPLER["cfg"]


@dataclass(frozen=True)
class InpaintBranch:
    """One masked re-sample of the shared source (mask: white = repaint)."""

    name: str
    prompt: str
    denoise: float
    mask_png: bytes
    negative: str = ""  # extra negatives for this branch only


InpaintPlan = Callable[[bytes], Awaitable[tuple[bytes, list[InpaintBranch]]]]
DEFAULT_SAMPLING = Sampling()
_SET_SETTLE_S = 0.25


def _inpaint_graph(
    source: str,
    branches: list[InpaintBranch],
    masks: dict[str, str],
    seed: int,
    sfw: bool,
    negative_extra: str | None,
    sampling: Sampling,
) -> tuple[dict, dict[str, str]]:
    """One ComfyUI graph: a shared checkpoint/LoRA load and source encode, and
    one masked KSampler -> VAEDecode -> SaveImage branch per expression.
    Returns the graph and {branch name: SaveImage node id}."""
    graph: dict = {
        "4": json.loads(json.dumps(WORKFLOW_TEMPLATE["4"])),
        "10": json.loads(json.dumps(WORKFLOW_TEMPLATE["10"])),
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative_prompt(sfw, negative_extra), "clip": ["10", 1]}},
        "11": {"class_type": "LoadImage", "inputs": {"image": source}},
        "12": {"class_type": "VAEEncode", "inputs": {"pixels": ["11", 0], "vae": ["4", 2]}},
        "16": {"class_type": "DifferentialDiffusion", "inputs": {"model": ["10", 0]}},
    }
    save_ids: dict[str, str] = {}
    for i, branch in enumerate(branches):
        n = 100 + 10 * i
        ids = [str(n + k) for k in range(6)]
        graph[ids[0]] = {"class_type": "CLIPTextEncode",
                         "inputs": {"text": branch.prompt, "clip": ["10", 1]}}
        negative = ["7", 0]
        if branch.negative:
            neg_id = str(n + 9)
            graph[neg_id] = {"class_type": "CLIPTextEncode", "inputs": {
                "text": f"{negative_prompt(sfw, negative_extra)}, {branch.negative}", "clip": ["10", 1]}}
            negative = [neg_id, 0]
        graph[ids[1]] = {"class_type": "LoadImageMask",
                         "inputs": {"image": masks[branch.name], "channel": "red"}}
        graph[ids[2]] = {"class_type": "SetLatentNoiseMask",
                         "inputs": {"samples": ["12", 0], "mask": [ids[1], 0]}}
        graph[ids[3]] = {"class_type": "KSampler", "inputs": {
            "seed": int(seed) % (2**32), "steps": sampling.steps, "cfg": sampling.cfg,
            "sampler_name": sampling.sampler, "scheduler": sampling.scheduler,
            "denoise": max(0.0, min(1.0, float(branch.denoise))),
            "model": ["16", 0], "positive": [ids[0], 0], "negative": negative,
            "latent_image": [ids[2], 0],
        }}
        graph[ids[4]] = {"class_type": "VAEDecode", "inputs": {"samples": [ids[3], 0], "vae": ["4", 2]}}
        graph[ids[5]] = {"class_type": "SaveImage",
                         "inputs": {"filename_prefix": f"klukai_set_{branch.name}", "images": [ids[4], 0]}}
        save_ids[branch.name] = ids[5]
    return graph, save_ids


OnImage = Callable[[str, bytes], Awaitable[None]]


async def _land_image(results: dict[str, bytes], on_image: OnImage | None, name: str, png: bytes) -> None:
    """An image of the set is done: record it, then let the caller show it."""
    results[name] = png
    if on_image is not None:
        await on_image(name, png)


async def _inpaint_set_base(
    lease: GPULease,
    results: dict[str, bytes],
    on_image: OnImage | None,
    *,
    base_png: bytes | None,
    base_prompt: str,
    width: int,
    height: int,
    sfw: bool,
    seed: int,
    negative_extra: str | None,
    sampling: Sampling,
) -> bytes | None:
    """The set's base: ``base_png`` as given, else a fresh txt2img render that
    lands as "base". None when that render failed."""
    if base_png is not None:
        return base_png
    base = await _run_workflow(_txt2img_workflow(
        base_prompt, width, height, sfw, seed, negative_extra, sampling,
    ), lease)
    if base is not None:
        await _land_image(results, on_image, "base", base)
    return base


async def _upload_inpaint_inputs(
    source: bytes, branches: list[InpaintBranch], lease: GPULease,
) -> tuple[str, dict[str, str]] | None:
    """Upload the shared source and one mask per branch, concurrently.
    Returns (source LoadImage name, {branch name: mask name}); None if any
    upload failed, since a graph with a missing input cannot run."""
    uploads = await asyncio.gather(
        _upload_image(source, lease), *(_upload_image(b.mask_png, lease) for b in branches)
    )
    source_name, mask_names = uploads[0], uploads[1:]
    if source_name is None or any(m is None for m in mask_names):
        return None
    return source_name, {b.name: str(m) for b, m in zip(branches, mask_names, strict=True)}


def _escalate_denoise(pending: list[InpaintBranch], attempt: int, step: float) -> list[InpaintBranch]:
    """``pending`` for this attempt: each denoise raised by ``attempt`` steps, capped at 1.0."""
    return [replace(b, denoise=min(1.0, b.denoise + attempt * step)) for b in pending]


async def _triage_renders(
    pending: list[InpaintBranch],
    outputs: dict[str, bytes],
    save_ids: dict[str, str],
    *,
    accept: Callable[[str, bytes], bool] | None,
    candidates: dict[str, list[bytes]],
    results: dict[str, bytes],
    on_image: OnImage | None,
) -> list[InpaintBranch]:
    """Sort one attempt's renders: an accepted one lands; a rejected one is
    kept as a candidate and returned for a re-run; a lost one is neither."""
    rejected: list[InpaintBranch] = []
    for branch in pending:
        png = outputs.get(save_ids[branch.name])
        if png is None:
            continue  # lost, not rejected: no point re-running it
        if accept is not None and not accept(branch.name, png):
            rejected.append(branch)
            candidates.setdefault(branch.name, []).append(png)
            continue
        await _land_image(results, on_image, branch.name, png)
    return rejected


async def _accept_fallbacks(
    pending: list[InpaintBranch],
    candidates: dict[str, list[bytes]],
    fallback: Callable[[str, list[bytes]], bytes | None] | None,
    results: dict[str, bytes],
    on_image: OnImage | None,
) -> None:
    """Retries exhausted: the caller may still take its best rejected render."""
    for branch in pending:
        chosen = fallback(branch.name, candidates[branch.name]) if fallback else None
        if chosen is None:
            continue
        logger.info("Accepting the best rejected render for %s", branch.name)
        await _land_image(results, on_image, branch.name, chosen)


async def _inpaint_set_inner(
    lease: GPULease,
    *,
    base_prompt: str,
    base_png: bytes | None,
    plan: InpaintPlan,
    seed: int,
    width: int,
    height: int,
    sfw: bool,
    negative_extra: str | None,
    base_sampling: Sampling,
    inpaint_sampling: Sampling,
    on_image: OnImage | None,
    accept: Callable[[str, bytes], bool] | None,
    retries: int,
    retry_denoise_step: float = 0.0,
    fallback: Callable[[str, list[bytes]], bytes | None] | None = None,
) -> dict[str, bytes]:
    """The leased body of generate_inpaint_set: base, plan, uploads, then the
    attempt loop. Every early return hands back what has landed so far, and
    the one VRAM free in ``finally`` is fail-closed (an unconfirmed free
    raises GPULeaseError so the lease is never returned as if clean)."""
    results: dict[str, bytes] = {}
    try:
        base = await _inpaint_set_base(
            lease, results, on_image, base_png=base_png, base_prompt=base_prompt,
            width=width, height=height, sfw=sfw, seed=seed, negative_extra=negative_extra,
            sampling=base_sampling,
        )
        if base is None:
            return results
        source, branches = await plan(base)
        if not branches:
            return results
        uploaded = await _upload_inpaint_inputs(source, branches, lease)
        if uploaded is None:
            return results
        source_name, masks = uploaded
        pending = list(branches)
        candidates: dict[str, list[bytes]] = {}
        for attempt in range(retries + 1):
            graph, save_ids = _inpaint_graph(
                source_name, _escalate_denoise(pending, attempt, retry_denoise_step), masks,
                seed + attempt, sfw, negative_extra, inpaint_sampling,
            )
            outputs = await _run_graph(graph, lease)
            rejected = await _triage_renders(
                pending, outputs, save_ids,
                accept=accept, candidates=candidates, results=results, on_image=on_image,
            )
            if not rejected:
                break
            pending = rejected
            if attempt < retries:
                logger.info("Re-running rejected inpaint branches: %s", [b.name for b in rejected])
        else:
            await _accept_fallbacks(pending, candidates, fallback, results, on_image)
        return results
    finally:
        # The graph's history is already complete, so a short settle suffices.
        if not await _free_comfyui_vram(lease, settle_s=_SET_SETTLE_S):
            raise GPULeaseError("ComfyUI VRAM cleanup could not be confirmed")
