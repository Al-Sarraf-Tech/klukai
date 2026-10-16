"""Authentication, bounded request decoding and HTTP response policy."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import ipaddress
import json
import secrets
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

_REQUEST_HEADER_ALLOWLIST = {
    "accept",
    "content-type",
    "idempotency-key",
    "openai-organization",
    "openai-project",
    "x-request-id",
}
_RESPONSE_HEADER_BLOCKLIST = {
    "connection",
    "content-encoding",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _error(status: int, message: str, error_type: str, code: str | int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "type": error_type,
            }
        },
    )


# This container's published port is bound only to the host's Tailscale
# address (`docker-proxy -host-ip 100.107.121.5 -host-port 1234 ...`).
# Docker's userland-proxy relays that connection into the container by
# opening its own connection from the host, sourced from the
# `dominus-ai-publish` network's gateway (172.19.0.1 — confirmed via
# tcpdump inside the container's netns, `docker network inspect
# dominus-ai-publish` for the IPAM gateway). A request from that specific
# address can only be docker-proxy relaying something that arrived on the
# Tailscale-bound port (itself gated by Tailscale's own device auth to
# join the tailnet) — sibling containers on this network reach each other
# with their own container IPs (172.19.0.3+), never the gateway address,
# so they're unaffected and still need the real token. If
# `dominus-ai-publish` is ever recreated with a different subnet, this
# constant needs updating to match. Also accept 127.0.0.1 and the tailnet
# CGNAT range directly, in case userland-proxy is disabled or the deploy
# topology changes so the real source IP (or loopback) is seen instead.
_TAILNET_RANGE = ipaddress.ip_network("100.64.0.0/10")
_DOCKER_PROXY_LOOPBACK = ipaddress.ip_address("127.0.0.1")
_DOCKER_PROXY_GATEWAY = ipaddress.ip_address("172.19.0.1")


def _from_tailnet(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    try:
        host = ipaddress.ip_address(client.host)
    except ValueError:
        return False
    return (
        host == _DOCKER_PROXY_LOOPBACK
        or host == _DOCKER_PROXY_GATEWAY
        or host in _TAILNET_RANGE
    )


# The token-less tailnet bypass exists for one reason (commit 36dcc7c): aichat
# on amarillo targets a literal LM Studio and has never sent a bearer. It only
# needs the OpenAI inference surface and the read-only catalog listings. The
# bypass therefore covers exactly that surface. Everything that changes GPU
# state -- model load/unload, the GPU lease, and the ComfyUI facade behind a
# lease token -- keeps requiring the real bearer from every source, because
# "tailnet" here also means any host-originated process (127.0.0.1) and any
# node that joined the tailnet. Deny prefixes win over allow prefixes; a path
# matching neither requires the token.
_TAILNET_BYPASS_ALLOW_PREFIXES = (
    "/v1/",
    "/api/v0/models",
    "/api/v1/models",
)
_TAILNET_BYPASS_DENY_PREFIXES = (
    "/api/v0/models/load",
    "/api/v0/models/unload",
    "/api/v1/models/load",
    "/api/v1/models/unload",
    "/api/v1/gpu",
    "/api/v1/comfy",
)


def _bypass_allowed(request: Request) -> bool:
    """Grant token-less access only to tailnet-looking sources, and only for
    the inference/catalog surface aichat needs."""

    if not _from_tailnet(request):
        return False
    path = request.url.path
    if path.startswith(_TAILNET_BYPASS_DENY_PREFIXES):
        return False
    return path.startswith(_TAILNET_BYPASS_ALLOW_PREFIXES)


def _authorized(request: Request, expected: str | None) -> bool:
    if expected is None:
        return True
    authorization = request.headers.get("authorization", "")
    scheme, separator, supplied = authorization.partition(" ")
    # Starlette decodes header bytes as latin-1, and compare_digest(str, str)
    # raises TypeError for non-ASCII input, which would surface as HTTP 500
    # instead of 401. Compare the raw header bytes against the UTF-8 token:
    # encoding the latin-1 string back to latin-1 recovers the exact bytes the
    # client sent, so a UTF-8 token still matches when the client sends it.
    return (
        bool(separator)
        and scheme.lower() == "bearer"
        and secrets.compare_digest(
            supplied.encode("latin-1", "replace"), expected.encode("utf-8")
        )
    )


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in _RESPONSE_HEADER_BLOCKLIST
    }


def _payload_too_large(limit: int) -> JSONResponse:
    return _error(
        413,
        f"Request body exceeds the gateway limit of {limit} bytes",
        "invalid_request_error",
        "payload_too_large",
    )


async def _bounded_body(request: Request, limit: int) -> bytes | JSONResponse:
    """Read the request body without letting the client size gateway memory.

    A declared Content-Length above the limit is refused before any body byte
    is read. A chunked (or lying) client is cut off as soon as the streamed
    total crosses the same limit. Callers must not read the body again.
    """

    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                return _payload_too_large(limit)
        except ValueError:
            # uvicorn/h11 reject a malformed Content-Length before the app
            # sees it; the streamed accounting below still applies regardless.
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            return _payload_too_large(limit)
        chunks.append(chunk)
    return b"".join(chunks)


async def _json_object(request: Request, limit: int) -> dict[str, Any] | JSONResponse:
    body = await _bounded_body(request, limit)
    if isinstance(body, JSONResponse):
        return body
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error(
            400, "Request body must be valid JSON", "invalid_request_error", 400
        )
    if not isinstance(payload, dict):
        return _error(
            400, "Request body must be a JSON object", "invalid_request_error", 400
        )
    return payload


def _upstream_failure(response: httpx.Response) -> Response:
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=_response_headers(response),
        media_type=None,
    )


@asynccontextmanager
async def _bounded_coordination_lock(
    lock: asyncio.Lock,
    timeout_seconds: float,
) -> AsyncIterator[bool]:
    """Wait a fixed period for in-flight inference without orphaning a lease."""

    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout_seconds)
    except TimeoutError:
        yield False
        return
    try:
        yield True
    finally:
        lock.release()
