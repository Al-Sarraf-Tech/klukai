"""Read-only catalog views and LM Studio model-management routes."""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from .catalog import Catalog
from .http_policy import _error, _json_object, _upstream_failure
from .model_state import _v0_model, _v1_model, _openai_model, _state, _post_management
from .runtime import GatewayRuntime


def register_model_routes(application: FastAPI, runtime: GatewayRuntime) -> None:
    @application.get("/api/v0/models")
    async def api_v0_models() -> dict[str, Any]:
        catalog, states = await runtime.catalog_states()
        return {
            "object": "list",
            "data": [
                _v0_model(model, _state(model, states)) for model in catalog.models
            ],
        }

    @application.get("/api/v0/models/{model_id:path}", response_model=None)
    async def api_v0_model(model_id: str) -> dict[str, Any] | JSONResponse:
        catalog, states = await runtime.catalog_states()
        model = catalog.resolve(model_id)
        if model is None:
            return _error(
                404,
                f"Unknown model: {model_id}",
                "invalid_request_error",
                "model_not_found",
            )
        return _v0_model(model, _state(model, states))

    @application.get("/api/v1/models")
    async def api_v1_models() -> dict[str, Any]:
        catalog, states = await runtime.catalog_states()
        return {
            "models": [
                _v1_model(model, _state(model, states)) for model in catalog.models
            ]
        }

    @application.get("/v1/models")
    async def openai_models() -> dict[str, Any]:
        catalog: Catalog = runtime.catalog
        return {
            "object": "list",
            "data": [_openai_model(model) for model in catalog.models],
        }

    async def manage_v0(request: Request, operation: str) -> Response | dict[str, Any]:
        payload = await _json_object(request, runtime.limits().MAX_REQUEST_BODY_BYTES)
        if isinstance(payload, JSONResponse):
            return payload
        if operation == "load":
            payload["ttl"] = runtime.limits().MODEL_IDLE_TTL_SECONDS
        resolved = runtime.requested_model(payload, "model")
        if isinstance(resolved, JSONResponse):
            return resolved
        model, requested_id = resolved
        async with runtime.coordination_lock:
            if operation == "load" and runtime.game_active(model):
                return runtime.game_block()
            if operation == "load" and (blocked := runtime.lease_block()) is not None:
                return blocked
            response = await _post_management(
                runtime.upstream,
                f"/models/{operation}",
                model.router_id,
            )
        if isinstance(response, JSONResponse):
            return response
        if response.is_error:
            return _upstream_failure(response)
        result: dict[str, Any] = {
            "success": True,
            "model": requested_id,
            "state": "loaded" if operation == "load" else "not-loaded",
        }
        if operation == "load":
            result["ttl"] = runtime.limits().MODEL_IDLE_TTL_SECONDS
        return result

    @application.post("/api/v0/models/load", response_model=None)
    async def api_v0_load(request: Request) -> Response | dict[str, Any]:
        return await manage_v0(request, "load")

    @application.post("/api/v0/models/unload", response_model=None)
    async def api_v0_unload(request: Request) -> Response | dict[str, Any]:
        return await manage_v0(request, "unload")

    @application.post("/api/v1/models/load", response_model=None)
    async def api_v1_load(request: Request) -> Response | dict[str, Any]:
        payload = await _json_object(request, runtime.limits().MAX_REQUEST_BODY_BYTES)
        if isinstance(payload, JSONResponse):
            return payload
        payload["ttl"] = runtime.limits().MODEL_IDLE_TTL_SECONDS
        resolved = runtime.requested_model(payload, "model")
        if isinstance(resolved, JSONResponse):
            return resolved
        model, requested_id = resolved
        started = time.monotonic()
        async with runtime.coordination_lock:
            if runtime.game_active(model):
                return runtime.game_block()
            if (blocked := runtime.lease_block()) is not None:
                return blocked
            response = await _post_management(
                runtime.upstream, "/models/load", model.router_id
            )
        if isinstance(response, JSONResponse):
            return response
        if response.is_error:
            return _upstream_failure(response)
        result: dict[str, Any] = {
            "type": model.v1_type,
            "model_instance_id": requested_id,
            "load_time_seconds": round(time.monotonic() - started, 3),
            "status": "loaded",
            "ttl": runtime.limits().MODEL_IDLE_TTL_SECONDS,
        }
        if payload.get("echo_load_config") is True:
            result["load_config"] = {
                "context_length": model.loaded_context_length
                if model.loaded_context_length is not None
                else model.max_context_length
            }
        return result

    @application.post("/api/v1/models/unload", response_model=None)
    async def api_v1_unload(request: Request) -> Response | dict[str, Any]:
        payload = await _json_object(request, runtime.limits().MAX_REQUEST_BODY_BYTES)
        if isinstance(payload, JSONResponse):
            return payload
        field = "instance_id" if "instance_id" in payload else "model"
        resolved = runtime.requested_model(payload, field)
        if isinstance(resolved, JSONResponse):
            return resolved
        model, requested_id = resolved
        async with runtime.coordination_lock:
            response = await _post_management(
                runtime.upstream, "/models/unload", model.router_id
            )
        if isinstance(response, JSONResponse):
            return response
        if response.is_error:
            return _upstream_failure(response)
        return {"instance_id": requested_id}
