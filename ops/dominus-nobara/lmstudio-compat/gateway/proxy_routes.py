"""Comfy capability admission and OpenAI response streaming."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from .catalog import Model
from .gpu_lease import LeaseTokenError, LeaseStateError
from .http_policy import (
    _bounded_body,
    _error,
    _response_headers,
    _REQUEST_HEADER_ALLOWLIST,
)
from .runtime import GatewayRuntime


def register_proxy_routes(application: FastAPI, runtime: GatewayRuntime) -> None:
    @application.api_route(
        "/api/v1/comfy/{endpoint:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy_comfyui(endpoint: str, request: Request) -> Response:
        body = await _bounded_body(request, runtime.limits().MAX_REQUEST_BODY_BYTES)
        if isinstance(body, JSONResponse):
            return body
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() in _REQUEST_HEADER_ALLOWLIST
        }
        token = request.headers.get("x-gpu-lease-token", "")
        if not token or len(token) > 256:
            return _error(
                403,
                "A matching active GPU lease token is required",
                "permission_error",
                "gpu_lease_token_invalid",
            )
        # Admission and the complete upstream request share the release/expiry
        # lock. Cleanup therefore cannot remove the marker and unblock LLMs
        # between capability validation and a delayed ComfyUI enqueue.
        async with runtime.coordination_lock:
            if runtime.game_active():
                return runtime.game_block()
            try:
                lease = runtime.lease_store.owned_by(token)
            except LeaseTokenError:
                return _error(
                    403,
                    "The token does not own the active GPU lease",
                    "permission_error",
                    "gpu_lease_token_invalid",
                )
            except (LeaseStateError, OSError):
                return _error(
                    503,
                    "The persistent GPU lease marker is unavailable or invalid",
                    "unavailable_error",
                    "gpu_lease_state_invalid",
                )
            if lease is None:
                return _error(
                    503,
                    "No active GPU lease permits a ComfyUI request",
                    "unavailable_error",
                    "gpu_lease_required",
                )
            if lease.state != "active" or lease.is_expired():
                return _error(
                    503,
                    "The GPU lease is no longer active for workload requests",
                    "unavailable_error",
                    "gpu_lease_not_active",
                )
            if lease.workload != "comfyui":
                return _error(
                    403,
                    "The active GPU lease belongs to another workload",
                    "permission_error",
                    "gpu_lease_workload_mismatch",
                )

            try:
                response = await runtime.comfyui.request(
                    request.method,
                    f"/{endpoint}",
                    params=tuple(request.query_params.multi_items()),
                    headers=headers,
                    content=body,
                    timeout=runtime.limits().COMFY_PROXY_REQUEST_SECONDS,
                )
            except httpx.TimeoutException:
                return _error(504, "ComfyUI timed out", "timeout_error", 504)
            except httpx.RequestError:
                return _error(502, "ComfyUI is unavailable", "upstream_error", 502)
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=_response_headers(response),
            media_type=None,
        )

    @application.api_route(
        "/v1/{endpoint:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy_openai(endpoint: str, request: Request) -> Response:
        body = await _bounded_body(request, runtime.limits().MAX_REQUEST_BODY_BYTES)
        if isinstance(body, JSONResponse):
            return body
        payload: dict[str, Any] | None = None
        model: Model | None = None
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if body and content_type == "application/json":
            try:
                parsed = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return _error(
                    400,
                    "Request body must be valid JSON",
                    "invalid_request_error",
                    400,
                )
            if isinstance(parsed, dict):
                payload = parsed
                payload.pop("ttl", None)
                model_name = payload.get("model")
                if isinstance(model_name, str):
                    model = runtime.catalog.resolve(model_name)
                    if model is None:
                        return _error(
                            404,
                            f"Unknown model: {model_name}",
                            "invalid_request_error",
                            "model_not_found",
                        )
                    payload["model"] = model.router_id
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() in _REQUEST_HEADER_ALLOWLIST
        }
        upstream_request = runtime.upstream.build_request(
            request.method,
            f"/v1/{endpoint}",
            params=tuple(request.query_params.multi_items()),
            headers=headers,
            content=body,
        )
        wants_stream = payload is not None and payload.get("stream") is True
        coordination_lock: asyncio.Lock = runtime.coordination_lock
        # Not _bounded_coordination_lock: the streaming path must keep holding
        # the lock after this function returns, until relay() finishes.
        try:
            await asyncio.wait_for(
                coordination_lock.acquire(),
                timeout=runtime.limits().INFERENCE_COORDINATION_WAIT_SECONDS,
            )
        except TimeoutError:
            busy = _error(
                503,
                "Another inference request is still in flight",
                "unavailable_error",
                "inference_busy",
            )
            busy.headers["Retry-After"] = "5"
            return busy
        if runtime.game_active(model):
            coordination_lock.release()
            return runtime.game_block()
        if (blocked := runtime.lease_block()) is not None:
            coordination_lock.release()
            return blocked
        try:
            if wants_stream:
                upstream_response = await runtime.upstream.send(
                    upstream_request, stream=True
                )
            else:
                upstream_response = await runtime.upstream.send(upstream_request)
        except httpx.TimeoutException:
            coordination_lock.release()
            return _error(504, "llama.cpp router timed out", "timeout_error", 504)
        except httpx.RequestError:
            coordination_lock.release()
            return _error(502, "llama.cpp router is unavailable", "upstream_error", 502)
        except BaseException:
            coordination_lock.release()
            raise

        if not wants_stream:
            result = Response(
                content=upstream_response.content,
                status_code=upstream_response.status_code,
                headers=_response_headers(upstream_response),
                media_type=None,
            )
            coordination_lock.release()
            return result

        if upstream_response.is_error:
            try:
                content = await upstream_response.aread()
            finally:
                try:
                    await upstream_response.aclose()
                finally:
                    coordination_lock.release()
            return Response(
                content=content,
                status_code=upstream_response.status_code,
                headers=_response_headers(upstream_response),
                media_type=None,
            )

        async def relay() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream_response.aiter_bytes():
                    yield chunk
            finally:
                try:
                    await upstream_response.aclose()
                finally:
                    coordination_lock.release()

        return StreamingResponse(
            relay(),
            status_code=upstream_response.status_code,
            headers=_response_headers(upstream_response),
            media_type=None,
        )
