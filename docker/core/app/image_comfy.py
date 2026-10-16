"""Authenticated ComfyUI transport using one injected shared HTTP client.

This module queues once, polls that accepted job, and downloads its outputs.
It never selects another provider, acquires a GPU lease or repeats admission.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
import hashlib
import logging
from typing import Any

import httpx

from .dependencies import http_request
from .gpu_lease import GPULease, gpu_lease_auth_headers

logger = logging.getLogger(__name__)


class ComfyTransport:
    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        poll: Iterable[int],
        fast_s: float,
        fast_count: int,
    ) -> None:
        self.client = client
        self.url = url
        self.poll = poll
        self.fast_s = fast_s
        self.fast_count = fast_count

    async def interrupt_comfyui(self, lease: GPULease) -> bool:
        """Interrupt the current ComfyUI generation."""
        try:
            response = await http_request(
                "comfyui",
                self.client,
                "post",
                f"{self.url}/interrupt",
                headers=gpu_lease_auth_headers(lease),
                timeout=5.0,
            )
            if response.status_code != 200:
                logger.warning(
                    "ComfyUI interrupt returned HTTP %s", response.status_code
                )
                return False
            logger.info("ComfyUI generation interrupted")
            await asyncio.sleep(1)  # Let it settle
            return True
        except Exception as e:
            logger.warning("ComfyUI interrupt failed: %s", e)
            return False

    async def free_comfyui_vram(self, lease: GPULease, settle_s: float = 2.0) -> bool:
        """Unload ComfyUI weights, retrying before the gateway lease is released.

        ``settle_s`` lets ComfyUI finish post-processing first; a caller that has
        already read the finished history (the inpaint set) can pass less.
        """
        await asyncio.sleep(settle_s)
        for attempt in range(1, 4):
            try:
                response = await http_request(
                    "comfyui",
                    self.client,
                    "post",
                    f"{self.url}/free",
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

    async def upload_image(self, png: bytes, lease: GPULease) -> str | None:
        """Upload a source image to ComfyUI's input folder; returns its LoadImage name."""
        try:
            r = await http_request(
                "comfyui",
                self.client,
                "post",
                f"{self.url}/upload/image",
                headers=gpu_lease_auth_headers(lease),
                # Content-addressed name + overwrite: re-uploading the same base or
                # mask reuses one input file instead of piling up copies.
                files={
                    "image": (
                        f"klukai_src_{hashlib.sha256(png).hexdigest()[:16]}.png",
                        png,
                        "image/png",
                    )
                },
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

    async def queue_and_wait(
        self, workflow: dict[str, Any], lease: GPULease
    ) -> dict[str, Any] | None:
        """Queue ``workflow`` and wait for its history; returns its outputs or None."""
        r = await http_request(
            "comfyui",
            self.client,
            "post",
            f"{self.url}/prompt",
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
        for i in self.poll:
            await asyncio.sleep(self.fast_s if i < self.fast_count else 1)
            r = await http_request(
                "comfyui",
                self.client,
                "get",
                f"{self.url}/history/{prompt_id}",
                headers=gpu_lease_auth_headers(lease),
            )
            if r.status_code == 200:
                history = r.json()
                if prompt_id in history:
                    return dict(history[prompt_id].get("outputs", {}))

        logger.warning("Image generation timed out waiting for ComfyUI history")
        return None

    async def fetch_image(self, img: dict[str, Any], lease: GPULease) -> bytes | None:
        r = await http_request(
            "comfyui",
            self.client,
            "get",
            f"{self.url}/view",
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

    async def run_workflow(
        self, workflow: dict[str, Any], lease: GPULease
    ) -> bytes | None:
        """Queue ``workflow``, poll its history, and fetch the first output image."""
        try:
            outputs = await self.queue_and_wait(workflow, lease)
            for output in (outputs or {}).values():
                images = output.get("images", [])
                if images:
                    return await self.fetch_image(images[0], lease)
            return None
        except Exception as e:
            logger.error("Image generation failed: %s", e)
            return None

    async def run_graph(
        self, workflow: dict[str, Any], lease: GPULease
    ) -> dict[str, bytes]:
        """Queue a multi-output graph; returns {SaveImage node id: first image}."""
        try:
            outputs = await self.queue_and_wait(workflow, lease)
            result: dict[str, bytes] = {}
            for node_id, output in (outputs or {}).items():
                images = output.get("images", [])
                if images:
                    data = await self.fetch_image(images[0], lease)
                    if data:
                        result[str(node_id)] = data
            return result
        except Exception as e:
            logger.error("Image graph failed: %s", e)
            return {}

    async def ready(self, lease: GPULease) -> bool:
        response = await http_request(
            "comfyui",
            self.client,
            "get",
            f"{self.url}/queue",
            headers=gpu_lease_auth_headers(lease),
            timeout=5.0,
        )
        if response.status_code != 200:
            return False
        data = response.json()
        running = len(data.get("queue_running", []))
        pending = len(data.get("queue_pending", []))
        if running > 0 or pending > 0:
            logger.info("ComfyUI busy: %d running, %d pending", running, pending)
            return False
        return True
