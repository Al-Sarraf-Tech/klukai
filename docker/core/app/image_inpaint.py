"""One-lease inpaint set orchestration with explicit transport callbacks."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
import logging
from typing import Any, Protocol, TypedDict, NotRequired

from .gpu_lease import GPULease, GPULeaseError
from .image_workflows import InpaintBranch, Sampling, _inpaint_graph, _txt2img_workflow

logger = logging.getLogger(__name__)
InpaintPlan = Callable[[bytes], Awaitable[tuple[bytes, list[InpaintBranch]]]]


class InpaintSetOptions(TypedDict):
    base_prompt: str
    base_png: bytes | None
    plan: InpaintPlan
    seed: int
    width: int
    height: int
    sfw: bool
    negative_extra: str | None
    base_sampling: Sampling
    inpaint_sampling: Sampling
    on_image: Callable[[str, bytes], Awaitable[None]] | None
    accept: Callable[[str, bytes], bool] | None
    retries: int
    retry_denoise_step: NotRequired[float]
    fallback: NotRequired[Callable[[str, list[bytes]], bytes | None] | None]


class FreeMemory(Protocol):
    async def __call__(self, lease: GPULease, settle_s: float = 2.0) -> bool: ...


@dataclass
class InpaintRenderer:
    upload: Callable[[bytes, GPULease], Awaitable[str | None]]
    run_workflow: Callable[[dict[str, Any], GPULease], Awaitable[bytes | None]]
    run_graph: Callable[[dict[str, Any], GPULease], Awaitable[dict[str, bytes]]]
    free: FreeMemory

    async def render(
        self,
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
        on_image: Callable[[str, bytes], Awaitable[None]] | None,
        accept: Callable[[str, bytes], bool] | None,
        retries: int,
        retry_denoise_step: float = 0.0,
        fallback: Callable[[str, list[bytes]], bytes | None] | None = None,
    ) -> dict[str, bytes]:
        results: dict[str, bytes] = {}
        try:
            base = base_png
            if base is None:
                base = await self.run_workflow(
                    _txt2img_workflow(
                        base_prompt,
                        width,
                        height,
                        sfw,
                        seed,
                        negative_extra,
                        base_sampling,
                    ),
                    lease,
                )
                if base is None:
                    return results
                results["base"] = base
                if on_image is not None:
                    await on_image("base", base)
            source, branches = await plan(base)
            if not branches:
                return results
            uploads = await asyncio.gather(
                self.upload(source, lease),
                *(self.upload(b.mask_png, lease) for b in branches),
            )
            source_name, mask_names = uploads[0], uploads[1:]
            if source_name is None or any(m is None for m in mask_names):
                return results
            masks = {b.name: str(m) for b, m in zip(branches, mask_names, strict=True)}
            return await self.render_batches(
                lease,
                source_name,
                branches,
                masks,
                seed,
                sfw,
                negative_extra,
                inpaint_sampling,
                on_image,
                accept,
                retries,
                retry_denoise_step,
                fallback,
                results,
            )
        finally:
            # The graph's history is already complete, so a short settle suffices.
            if not await self.free(lease, settle_s=0.25):
                raise GPULeaseError("ComfyUI VRAM cleanup could not be confirmed")

    async def render_batches(
        self,
        lease: GPULease,
        source_name: str,
        branches: list[InpaintBranch],
        masks: dict[str, str],
        seed: int,
        sfw: bool,
        negative_extra: str | None,
        inpaint_sampling: Sampling,
        on_image: Callable[[str, bytes], Awaitable[None]] | None,
        accept: Callable[[str, bytes], bool] | None,
        retries: int,
        retry_denoise_step: float,
        fallback: Callable[[str, list[bytes]], bytes | None] | None,
        results: dict[str, bytes],
    ) -> dict[str, bytes]:
        pending = list(branches)
        candidates: dict[str, list[bytes]] = {}
        for attempt in range(retries + 1):
            escalated = [
                replace(b, denoise=min(1.0, b.denoise + attempt * retry_denoise_step))
                for b in pending
            ]
            graph, save_ids = _inpaint_graph(
                source_name,
                escalated,
                masks,
                seed + attempt,
                sfw,
                negative_extra,
                inpaint_sampling,
            )
            outputs = await self.run_graph(graph, lease)
            rejected: list[InpaintBranch] = []
            for branch in pending:
                png = outputs.get(save_ids[branch.name])
                if png is None:
                    continue  # lost, not rejected: no point re-running it
                if accept is not None and not accept(branch.name, png):
                    rejected.append(branch)
                    candidates.setdefault(branch.name, []).append(png)
                    continue
                results[branch.name] = png
                if on_image is not None:
                    await on_image(branch.name, png)
            if not rejected:
                break
            pending = rejected
            if attempt < retries:
                logger.info(
                    "Re-running rejected inpaint branches: %s",
                    [b.name for b in rejected],
                )
        else:
            # Retries exhausted: the caller may still take its best rejected render.
            for branch in pending:
                chosen = (
                    fallback(branch.name, candidates[branch.name]) if fallback else None
                )
                if chosen is None:
                    continue
                logger.info("Accepting the best rejected render for %s", branch.name)
                results[branch.name] = chosen
                if on_image is not None:
                    await on_image(branch.name, chosen)
        return results
