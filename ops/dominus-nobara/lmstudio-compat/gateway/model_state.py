"""Catalog presentation and the bounded llama.cpp model-control protocol."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from fastapi.responses import JSONResponse, Response
from .catalog import Catalog, Model, quantization_bits, router_state
from .http_policy import _error, _upstream_failure
from .limits import Limits


async def _router_states(
    client: httpx.AsyncClient, catalog: Catalog, timeout_seconds: float
) -> tuple[dict[str, str], bool]:
    try:
        response = await client.get("/models", timeout=timeout_seconds)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError):
        return {}, False

    if isinstance(payload, dict):
        entries = payload.get("data", payload.get("models", []))
    elif isinstance(payload, list):
        entries = payload
    else:
        return {}, False
    if not isinstance(entries, list):
        return {}, False

    states: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("id", entry.get("model", entry.get("name")))
        if not isinstance(identifier, str):
            continue
        model = catalog.resolve(identifier)
        if model is None:
            continue
        state_value = entry.get("status", entry.get("state", "not-loaded"))
        states[model.id] = router_state(state_value)
    return states, True


def _state(model: Model, states: dict[str, str]) -> str:
    return states.get(model.id, model.state_hint)


def _v0_model(model: Model, state: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": model.id,
        "object": "model",
        "type": model.v0_type,
        "publisher": model.publisher,
        "arch": model.architecture,
        "compatibility_type": model.compatibility_type,
        "quantization": model.quantization,
        "state": state,
        "max_context_length": model.max_context_length,
    }
    if model.aliases:
        result["aliases"] = list(model.aliases)
    capabilities = model.v0_capabilities()
    if capabilities:
        result["capabilities"] = capabilities
    return result


def _v1_model(model: Model, state: str) -> dict[str, Any]:
    loaded_instances: list[dict[str, Any]] = []
    if state == "loaded":
        loaded_instances.append(
            {
                "id": model.id,
                "config": {
                    "context_length": model.loaded_context_length
                    if model.loaded_context_length is not None
                    else model.max_context_length
                },
            }
        )
    result: dict[str, Any] = {
        "type": model.v1_type,
        "publisher": model.publisher,
        "key": model.id,
        "display_name": model.display_name,
        "quantization": {
            "name": model.quantization,
            "bits_per_weight": quantization_bits(model.quantization),
        },
        "size_bytes": model.size_bytes,
        "params_string": model.params_string,
        "loaded_instances": loaded_instances,
        "max_context_length": model.max_context_length,
        "format": model.compatibility_type,
    }
    if model.architecture is not None and model.v1_type == "llm":
        result["architecture"] = model.architecture
    if model.aliases:
        result["aliases"] = list(model.aliases)
    capabilities = model.v1_capabilities()
    if capabilities is not None:
        result["capabilities"] = capabilities
    return result


def _openai_model(model: Model) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": model.id,
        "object": "model",
        "created": 0,
        "owned_by": model.publisher,
    }
    if model.aliases:
        result["aliases"] = list(model.aliases)
    return result


async def _post_management(
    client: httpx.AsyncClient,
    path: str,
    router_id: str,
    timeout_seconds: float | None = None,
) -> httpx.Response | JSONResponse:
    try:
        kwargs: dict[str, Any] = {"json": {"model": router_id}}
        if timeout_seconds is not None:
            kwargs["timeout"] = timeout_seconds
        return await client.post(path, **kwargs)
    except httpx.TimeoutException:
        return _error(504, "llama.cpp router timed out", "timeout_error", 504)
    except httpx.RequestError:
        return _error(502, "llama.cpp router is unavailable", "upstream_error", 502)


def _router_entries(payload: object) -> list[object] | None:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        entries = payload.get("data", payload.get("models"))
        return entries if isinstance(entries, list) else None
    return None


def _raw_router_state(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("value")
    return str(value).strip().lower()


async def _resident_router_models(
    client: httpx.AsyncClient,
    catalog: Catalog,
    timeout_seconds: float,
) -> list[Model] | Response:
    try:
        response = await client.get("/models", timeout=timeout_seconds)
    except httpx.TimeoutException:
        return _error(504, "llama.cpp router timed out", "timeout_error", 504)
    except httpx.RequestError:
        return _error(502, "llama.cpp router is unavailable", "upstream_error", 502)
    if response.is_error:
        return _upstream_failure(response)
    try:
        entries = _router_entries(response.json())
    except ValueError:
        entries = None
    if entries is None:
        return _error(
            502,
            "llama.cpp returned an invalid model-state document",
            "upstream_error",
            "router_state_invalid",
        )

    nonresident_states = {"not-loaded", "not_loaded", "unloaded", "sleeping", "idle"}
    resident: list[Model] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return _error(
                502,
                "llama.cpp returned an invalid model-state entry",
                "upstream_error",
                "router_state_invalid",
            )
        identifier = entry.get("id", entry.get("model", entry.get("name")))
        state = _raw_router_state(entry.get("status", entry.get("state", "unknown")))
        if state in nonresident_states:
            continue
        if not isinstance(identifier, str):
            return _error(
                502,
                "llama.cpp reported resident state without a model identifier",
                "upstream_error",
                "router_state_invalid",
            )
        model = catalog.resolve(identifier)
        if model is None:
            return _error(
                502,
                "llama.cpp reported a model outside the immutable catalog",
                "upstream_error",
                "router_state_invalid",
            )
        if model not in resident:
            resident.append(model)
    return resident


async def _quiesce_router_within_deadline(
    limits: Limits,
    client: httpx.AsyncClient,
    catalog: Catalog,
    timeout_seconds: float,
) -> Response | None:
    resident = await _resident_router_models(client, catalog, timeout_seconds)
    if isinstance(resident, Response):
        return resident
    if len(resident) > limits.MAX_RESIDENT_ROUTER_MODELS:
        return _error(
            503,
            "llama.cpp reported more resident models than the one-model GPU contract permits",
            "unavailable_error",
            "router_state_invalid",
        )
    for model in resident:
        response = await _post_management(
            client,
            "/models/unload",
            model.router_id,
            timeout_seconds,
        )
        if isinstance(response, JSONResponse):
            return response
        if response.is_error:
            return _upstream_failure(response)
    deadline = time.monotonic() + limits.ROUTER_UNLOAD_DRAIN_SECONDS
    while True:
        remaining = await _resident_router_models(client, catalog, timeout_seconds)
        if isinstance(remaining, Response):
            return remaining
        if not remaining:
            return None
        if time.monotonic() >= deadline:
            return _error(
                503,
                "llama.cpp still has a resident model after unload",
                "unavailable_error",
                "router_not_quiesced",
            )
        await asyncio.sleep(limits.ROUTER_UNLOAD_POLL_SECONDS)


async def _quiesce_router(
    limits: Limits,
    client: httpx.AsyncClient,
    catalog: Catalog,
    timeout_seconds: float,
) -> Response | None:
    """Bound the complete router inspection/unload/confirmation sequence."""

    try:
        async with asyncio.timeout(limits.LEASE_ROUTER_QUIESCE_SECONDS):
            return await _quiesce_router_within_deadline(
                limits,
                client,
                catalog,
                timeout_seconds,
            )
    except TimeoutError:
        return _error(
            504,
            "llama.cpp router quiescence exceeded its bounded deadline",
            "timeout_error",
            "router_quiesce_timeout",
        )
