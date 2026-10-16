"""Typed network boundary adapters; no retries, substitution or hidden work."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from .circuit_breakers import get_breaker


class _ServerUnavailable(Exception):
    """Internal accounting only; response contracts remain caller-owned."""


async def http_request(
    dep: str,
    client: httpx.AsyncClient | None,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    """Protect one request, preserving non-200 responses and retry ownership."""
    if client is None:
        raise RuntimeError(f"{dep} client not initialized")
    try:
        async with get_breaker(dep).protect():
            response: httpx.Response = await getattr(client, method)(url, **kwargs)
            if isinstance(response.status_code, int) and response.status_code >= 500:
                raise _ServerUnavailable()
    except _ServerUnavailable:
        return response
    return response


@asynccontextmanager
async def http_stream(
    dep: str,
    client: httpx.AsyncClient | None,
    method: str,
    url: str,
    **kwargs: Any,
) -> AsyncIterator[httpx.Response]:
    """Keep admission until body consumption exits, including cancellation."""
    if client is None:
        raise RuntimeError(f"{dep} client not initialized")
    try:
        async with get_breaker(dep).protect():
            async with client.stream(method, url, **kwargs) as response:
                yield response
                if (
                    isinstance(response.status_code, int)
                    and response.status_code >= 500
                ):
                    raise _ServerUnavailable()
    except _ServerUnavailable:
        pass


def json_object(payload: object) -> dict[str, Any]:
    """Validate an external JSON object before admitting it into client state."""
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) for key in payload
    ):
        raise ValueError("Expected a JSON object")
    return payload


def json_objects(payload: object) -> list[dict[str, Any]]:
    """Validate a JSON array of objects without leaking unchecked containers."""
    if not isinstance(payload, list):
        raise ValueError("Expected a JSON array")
    return [json_object(item) for item in payload]
