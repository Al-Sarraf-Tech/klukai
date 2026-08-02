"""Lease-aware client helpers for the GPU-backed companion voice service."""

from __future__ import annotations

import os
from typing import Any

import httpx

from .gpu_lease import gpu_lease, gpu_lease_capability_headers


def voice_url() -> str:
    return os.environ.get("VOICE_URL", "http://100.107.121.5:8301").rstrip("/")


async def post_leased_tts(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
    *,
    timeout: float | None = None,
) -> httpx.Response:
    """Run one XTTS request while all local and remote LLM loads are excluded."""

    # Import lazily to avoid a helpers -> image_gen -> llm_router import cycle.
    from .llm_router import get_lm_gate
    from .helpers import voice_auth_headers

    async with get_lm_gate():
        async with gpu_lease("companion-voice") as lease:
            headers = {
                **voice_auth_headers(),
                **gpu_lease_capability_headers(lease),
            }
            return await client.post(
                f"{voice_url()}/tts",
                json=payload,
                headers=headers,
                timeout=timeout,
            )
