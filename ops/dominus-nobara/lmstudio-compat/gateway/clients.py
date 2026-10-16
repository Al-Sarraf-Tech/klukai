"""Transport construction for the three private upstream services."""

from __future__ import annotations

import httpx
from .settings import Settings


def _default_client(settings: Settings) -> httpx.AsyncClient:
    headers: dict[str, str] = {}
    if settings.upstream_token is not None:
        headers["Authorization"] = f"Bearer {settings.upstream_token}"
    return httpx.AsyncClient(
        base_url=settings.upstream_url,
        headers=headers,
        timeout=httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.read_timeout_seconds,
            write=settings.connect_timeout_seconds,
            pool=settings.connect_timeout_seconds,
        ),
        follow_redirects=False,
    )


def _default_comfy_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.comfyui_url,
        timeout=httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.read_timeout_seconds,
            write=settings.connect_timeout_seconds,
            pool=settings.connect_timeout_seconds,
        ),
        follow_redirects=False,
    )


def _default_voice_client(settings: Settings) -> httpx.AsyncClient:
    headers: dict[str, str] = {}
    if settings.companion_voice_token is not None:
        headers["Authorization"] = f"Bearer {settings.companion_voice_token}"
    return httpx.AsyncClient(
        base_url=settings.companion_voice_url,
        headers=headers,
        timeout=httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.read_timeout_seconds,
            write=settings.connect_timeout_seconds,
            pool=settings.connect_timeout_seconds,
        ),
        follow_redirects=False,
    )
