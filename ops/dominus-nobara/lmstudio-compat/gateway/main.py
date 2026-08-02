"""LM Studio API facade over a llama.cpp multi-model router."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
import json
import logging
import secrets
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
import httpx

from .catalog import Catalog, Model, quantization_bits, router_state
from .gpu_lease import (
    LEASE_DEFAULT_SECONDS,
    Lease,
    LeaseBusyError,
    LeaseStateError,
    LeaseStore,
    LeaseTokenError,
)
from .settings import Settings


ClientFactory = Callable[[Settings], httpx.AsyncClient]
logger = logging.getLogger(__name__)

# The client-visible ceiling is 900 seconds. The pinned router is launched at
# 898 seconds because its roughly one-second scheduler needs a safety margin
# to guarantee actual model residency never exceeds this policy ceiling.
MODEL_IDLE_TTL_SECONDS = 900
NATIVE_VLLM_ACK_TIMEOUT_SECONDS = 15.0
LEASE_ACQUIRE_COORDINATION_WAIT_SECONDS = 30.0
LEASE_RELEASE_COORDINATION_WAIT_SECONDS = 10.0
LEASE_ROUTER_REQUEST_SECONDS = 5.0
LEASE_ROUTER_QUIESCE_SECONDS = 20.0
# llama.cpp's /models/unload frees VRAM after the call returns, not within it;
# an immediate re-check can still observe the outgoing model. Poll instead of
# failing on the first sample, the same way the Comfy queue drain does below.
ROUTER_UNLOAD_DRAIN_SECONDS = 8.0
ROUTER_UNLOAD_POLL_SECONDS = 0.2
LEASE_EXPIRY_POLL_SECONDS = 0.5
LEASE_CLEANUP_RETRY_SECONDS = 5.0
COMFY_QUEUE_DRAIN_SECONDS = 10.0
COMFY_QUEUE_POLL_SECONDS = 0.1
COMFY_CLEANUP_REQUEST_SECONDS = 5.0
VOICE_CLEANUP_REQUEST_SECONDS = 15.0
GPU_WORKLOAD_CLEANUP_SECONDS = 60.0
COMFY_PROXY_REQUEST_SECONDS = 120.0
MAX_RESIDENT_ROUTER_MODELS = 1

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


def _authorized(request: Request, expected: str | None) -> bool:
    if expected is None:
        return True
    authorization = request.headers.get("authorization", "")
    scheme, separator, supplied = authorization.partition(" ")
    return (
        bool(separator)
        and scheme.lower() == "bearer"
        and secrets.compare_digest(supplied, expected)
    )


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


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in _RESPONSE_HEADER_BLOCKLIST
    }


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


async def _json_object(request: Request) -> dict[str, Any] | JSONResponse:
    try:
        payload = await request.json()
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
    client: httpx.AsyncClient,
    catalog: Catalog,
    timeout_seconds: float,
) -> Response | None:
    resident = await _resident_router_models(client, catalog, timeout_seconds)
    if isinstance(resident, Response):
        return resident
    if len(resident) > MAX_RESIDENT_ROUTER_MODELS:
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
    deadline = time.monotonic() + ROUTER_UNLOAD_DRAIN_SECONDS
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
        await asyncio.sleep(ROUTER_UNLOAD_POLL_SECONDS)


async def _quiesce_router(
    client: httpx.AsyncClient,
    catalog: Catalog,
    timeout_seconds: float,
) -> Response | None:
    """Bound the complete router inspection/unload/confirmation sequence."""

    try:
        async with asyncio.timeout(LEASE_ROUTER_QUIESCE_SECONDS):
            return await _quiesce_router_within_deadline(
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


def _comfy_queue_is_empty(payload: object) -> bool | None:
    if not isinstance(payload, dict):
        return None
    running = payload.get("queue_running")
    pending = payload.get("queue_pending")
    if not isinstance(running, list) or not isinstance(pending, list):
        return None
    return not running and not pending


async def _cleanup_comfyui(client: httpx.AsyncClient) -> bool:
    """Interrupt, clear, drain, and unload ComfyUI before releasing the GPU."""

    try:
        interrupted = await client.post(
            "/interrupt", timeout=COMFY_CLEANUP_REQUEST_SECONDS
        )
        if interrupted.status_code != 200:
            return False
        cleared = await client.post(
            "/queue",
            json={"clear": True},
            timeout=COMFY_CLEANUP_REQUEST_SECONDS,
        )
        if cleared.status_code != 200:
            return False

        deadline = time.monotonic() + COMFY_QUEUE_DRAIN_SECONDS
        while True:
            queue = await client.get("/queue", timeout=COMFY_CLEANUP_REQUEST_SECONDS)
            if queue.status_code != 200:
                return False
            try:
                empty = _comfy_queue_is_empty(queue.json())
            except (TypeError, ValueError):
                return False
            if empty is None:
                return False
            if empty:
                break
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(COMFY_QUEUE_POLL_SECONDS)

        freed = await client.post(
            "/free",
            json={"unload_models": True, "free_memory": True},
            timeout=COMFY_CLEANUP_REQUEST_SECONDS,
        )
        if freed.status_code != 200:
            return False
        confirmation = await client.get(
            "/queue", timeout=COMFY_CLEANUP_REQUEST_SECONDS
        )
        if confirmation.status_code != 200:
            return False
        try:
            return _comfy_queue_is_empty(confirmation.json()) is True
        except (TypeError, ValueError):
            return False
    except Exception:
        return False


async def _cleanup_companion_voice(client: httpx.AsyncClient) -> bool:
    """Unload XTTS and confirm it is non-resident before lease removal."""

    try:
        response = await client.post(
            "/unload", timeout=VOICE_CLEANUP_REQUEST_SECONDS
        )
        if response.status_code != 200:
            return False
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return False
        if (
            not isinstance(payload, dict)
            or payload.get("status") != "ok"
            or payload.get("tts_loaded") is not False
        ):
            return False
        health = await client.get(
            "/health", timeout=COMFY_CLEANUP_REQUEST_SECONDS
        )
        if health.status_code != 200:
            return False
        try:
            health_payload = health.json()
        except (TypeError, ValueError):
            return False
        return (
            isinstance(health_payload, dict)
            and health_payload.get("tts_ready") is False
        )
    except Exception:
        return False


async def _cleanup_all_gpu_workloads(
    comfyui: httpx.AsyncClient,
    companion_voice: httpx.AsyncClient,
) -> bool:
    """Prove both non-LLM GPU services are empty without short-circuiting."""

    try:
        async with asyncio.timeout(GPU_WORKLOAD_CLEANUP_SECONDS):
            comfy_clean = await _cleanup_comfyui(comfyui)
            voice_clean = await _cleanup_companion_voice(companion_voice)
            return comfy_clean and voice_clean
    except TimeoutError:
        return False


async def _cleanup_and_complete_lease(
    lease_store: LeaseStore,
    lease: Lease,
    comfyui: httpx.AsyncClient,
    companion_voice: httpx.AsyncClient,
) -> bool:
    """Keep the marker fail-closed until its workload cleaner proves success."""

    try:
        lease_store.mark_state(lease.lease_id, "cleaning")
    except (LeaseStateError, OSError):
        return False

    try:
        cleaned = await _cleanup_all_gpu_workloads(comfyui, companion_voice)
    except Exception:
        cleaned = False
    if cleaned:
        try:
            lease_store.complete_cleanup(lease.lease_id)
            return True
        except (LeaseStateError, OSError):
            return False

    try:
        lease_store.mark_state(lease.lease_id, "cleanup_failed")
    except (LeaseStateError, OSError):
        pass
    return False


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


def create_app(
    settings: Settings | None = None,
    client_factory: ClientFactory | None = None,
    comfy_client_factory: ClientFactory | None = None,
    voice_client_factory: ClientFactory | None = None,
) -> FastAPI:
    config = settings or Settings.from_env()
    make_client = client_factory or _default_client
    make_comfy_client = comfy_client_factory or _default_comfy_client
    make_voice_client = voice_client_factory or _default_voice_client

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.catalog = Catalog.from_path(config.catalog_path)
        application.state.upstream = make_client(config)
        application.state.comfyui = make_comfy_client(config)
        application.state.companion_voice = make_voice_client(config)
        application.state.coordination_lock = asyncio.Lock()
        application.state.lease_store = LeaseStore(
            config.gpu_lease_marker_path, config.gpu_lease_ack_path
        )

        async def expire_stale_lease() -> None:
            last_attempted_lease_id: str | None = None
            last_attempt_monotonic = 0.0
            while True:
                async with application.state.coordination_lock:
                    try:
                        lease = application.state.lease_store.active()
                    except LeaseStateError:
                        # A malformed marker is intentionally fail-closed and
                        # requires operator inspection; never delete it here.
                        lease = None
                    if lease is None:
                        last_attempted_lease_id = None
                        last_attempt_monotonic = 0.0
                    elif lease.is_expired() or lease.state != "active":
                        now = time.monotonic()
                        retry_due = (
                            lease.lease_id != last_attempted_lease_id
                            or now - last_attempt_monotonic
                            >= LEASE_CLEANUP_RETRY_SECONDS
                        )
                        if retry_due:
                            last_attempted_lease_id = lease.lease_id
                            last_attempt_monotonic = now
                            cleaned = await _cleanup_and_complete_lease(
                                application.state.lease_store,
                                lease,
                                application.state.comfyui,
                                application.state.companion_voice,
                            )
                            if not cleaned:
                                logger.error(
                                    "GPU lease cleanup remains fail-closed for %s",
                                    lease.workload,
                                )
                await asyncio.sleep(LEASE_EXPIRY_POLL_SECONDS)

        expiry_task = asyncio.create_task(expire_stale_lease())
        try:
            yield
        finally:
            expiry_task.cancel()
            with suppress(asyncio.CancelledError):
                await expiry_task
            await application.state.companion_voice.aclose()
            await application.state.comfyui.aclose()
            await application.state.upstream.aclose()

    application = FastAPI(
        title="Dominus LM Studio compatibility gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def bearer_authentication(request: Request, call_next: Any) -> Response:
        if request.url.path == "/health" or _authorized(request, config.gateway_token):
            return await call_next(request)
        response = _error(
            401,
            "Missing or invalid bearer token",
            "authentication_error",
            401,
        )
        response.headers["WWW-Authenticate"] = "Bearer"
        return response

    def game_active() -> bool:
        return config.game_marker_path.exists()

    def game_block() -> JSONResponse:
        return _error(
            503,
            "Inference and model loading are disabled while a game is active",
            "unavailable_error",
            "game_active",
        )

    def lease_block() -> JSONResponse | None:
        try:
            lease = application.state.lease_store.active()
        except LeaseStateError:
            return _error(
                503,
                "The persistent GPU lease marker is invalid; inference is fail-closed",
                "unavailable_error",
                "gpu_lease_state_invalid",
            )
        if lease is None:
            return None
        response = _error(
            503,
            f"The GPU is leased to the {lease.workload} workload",
            "unavailable_error",
            "gpu_leased",
        )
        response.headers["Retry-After"] = str(max(1, lease.remaining_seconds()))
        return response

    async def catalog_states() -> tuple[Catalog, dict[str, str]]:
        catalog: Catalog = application.state.catalog
        states, _ = await _router_states(
            application.state.upstream, catalog, config.health_timeout_seconds
        )
        return catalog, states

    def requested_model(
        payload: dict[str, Any], field: str
    ) -> tuple[Model, str] | JSONResponse:
        identifier = payload.get(field)
        if not isinstance(identifier, str) or not identifier:
            return _error(
                400,
                f"{field} must be a non-empty string",
                "invalid_request_error",
                400,
            )
        catalog: Catalog = application.state.catalog
        model = catalog.resolve(identifier)
        if model is None:
            return _error(
                404,
                f"Unknown model: {identifier}",
                "invalid_request_error",
                "model_not_found",
            )
        return model, identifier

    @application.get("/health")
    async def health() -> JSONResponse:
        upstream_status = "unavailable"
        comfyui_status = "unavailable"
        try:
            response = await application.state.upstream.get(
                "/health", timeout=config.health_timeout_seconds
            )
            if response.status_code == 200:
                upstream_status = "ok"
            elif response.status_code == 503:
                upstream_status = "loading"
            else:
                upstream_status = "error"
        except httpx.HTTPError:
            pass
        try:
            comfy_response = await application.state.comfyui.get(
                "/system_stats", timeout=config.health_timeout_seconds
            )
            if comfy_response.status_code == 200:
                comfyui_status = "ok"
            else:
                comfyui_status = "error"
        except httpx.HTTPError:
            pass
        lease_state = "inactive"
        lease_workload: str | None = None
        lease_remaining_seconds = 0
        try:
            lease = application.state.lease_store.active()
        except LeaseStateError:
            lease_state = "invalid"
        else:
            if lease is not None:
                lease_state = (
                    "expired"
                    if lease.is_expired() and lease.state == "active"
                    else lease.state
                )
                lease_workload = lease.workload
                lease_remaining_seconds = lease.remaining_seconds()
        return JSONResponse(
            {
                "status": "ok" if upstream_status == "ok" else "degraded",
                "catalog": "ok",
                "models": len(application.state.catalog.models),
                "upstream": upstream_status,
                "comfyui_status": comfyui_status,
                "game_active": game_active(),
                "gpu_lease_state": lease_state,
                "gpu_lease_active": lease_state != "inactive",
                "gpu_lease_workload": lease_workload,
                "gpu_lease_remaining_seconds": lease_remaining_seconds,
                "model_idle_ttl_seconds": MODEL_IDLE_TTL_SECONDS,
            }
        )

    @application.get("/api/v0/models")
    async def api_v0_models() -> dict[str, Any]:
        catalog, states = await catalog_states()
        return {
            "object": "list",
            "data": [
                _v0_model(model, _state(model, states)) for model in catalog.models
            ],
        }

    @application.get("/api/v0/models/{model_id:path}", response_model=None)
    async def api_v0_model(model_id: str) -> dict[str, Any] | JSONResponse:
        catalog, states = await catalog_states()
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
        catalog, states = await catalog_states()
        return {
            "models": [
                _v1_model(model, _state(model, states)) for model in catalog.models
            ]
        }

    @application.get("/v1/models")
    async def openai_models() -> dict[str, Any]:
        catalog: Catalog = application.state.catalog
        return {
            "object": "list",
            "data": [_openai_model(model) for model in catalog.models],
        }

    async def manage_v0(request: Request, operation: str) -> Response | dict[str, Any]:
        payload = await _json_object(request)
        if isinstance(payload, JSONResponse):
            return payload
        if operation == "load":
            payload["ttl"] = MODEL_IDLE_TTL_SECONDS
        resolved = requested_model(payload, "model")
        if isinstance(resolved, JSONResponse):
            return resolved
        model, requested_id = resolved
        async with application.state.coordination_lock:
            if operation == "load" and game_active():
                return game_block()
            if operation == "load" and (blocked := lease_block()) is not None:
                return blocked
            response = await _post_management(
                application.state.upstream,
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
            result["ttl"] = MODEL_IDLE_TTL_SECONDS
        return result

    @application.post("/api/v0/models/load", response_model=None)
    async def api_v0_load(request: Request) -> Response | dict[str, Any]:
        return await manage_v0(request, "load")

    @application.post("/api/v0/models/unload", response_model=None)
    async def api_v0_unload(request: Request) -> Response | dict[str, Any]:
        return await manage_v0(request, "unload")

    @application.post("/api/v1/models/load", response_model=None)
    async def api_v1_load(request: Request) -> Response | dict[str, Any]:
        payload = await _json_object(request)
        if isinstance(payload, JSONResponse):
            return payload
        payload["ttl"] = MODEL_IDLE_TTL_SECONDS
        resolved = requested_model(payload, "model")
        if isinstance(resolved, JSONResponse):
            return resolved
        model, requested_id = resolved
        started = time.monotonic()
        async with application.state.coordination_lock:
            if game_active():
                return game_block()
            if (blocked := lease_block()) is not None:
                return blocked
            response = await _post_management(
                application.state.upstream, "/models/load", model.router_id
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
            "ttl": MODEL_IDLE_TTL_SECONDS,
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
        payload = await _json_object(request)
        if isinstance(payload, JSONResponse):
            return payload
        field = "instance_id" if "instance_id" in payload else "model"
        resolved = requested_model(payload, field)
        if isinstance(resolved, JSONResponse):
            return resolved
        model, requested_id = resolved
        async with application.state.coordination_lock:
            response = await _post_management(
                application.state.upstream, "/models/unload", model.router_id
            )
        if isinstance(response, JSONResponse):
            return response
        if response.is_error:
            return _upstream_failure(response)
        return {"instance_id": requested_id}

    @application.post("/api/v1/gpu/lease/acquire", response_model=None)
    async def acquire_gpu_lease(request: Request) -> Response | dict[str, Any]:
        payload = await _json_object(request)
        if isinstance(payload, JSONResponse):
            return payload
        if payload.get("owner") != "klukai-core":
            return _error(
                400,
                "owner must be klukai-core",
                "invalid_request_error",
                "gpu_lease_owner_invalid",
            )
        workload = payload.get("workload")
        if not isinstance(workload, str):
            return _error(
                400,
                "workload must be a lowercase slug",
                "invalid_request_error",
                "gpu_lease_workload_invalid",
            )
        requested_ttl = payload.get("ttl_seconds", LEASE_DEFAULT_SECONDS)
        if (
            isinstance(requested_ttl, bool)
            or not isinstance(requested_ttl, int)
            or requested_ttl <= 0
        ):
            return _error(
                400,
                "ttl_seconds must be a positive integer",
                "invalid_request_error",
                "gpu_lease_ttl_invalid",
            )

        async with _bounded_coordination_lock(
            application.state.coordination_lock,
            LEASE_ACQUIRE_COORDINATION_WAIT_SECONDS,
        ) as coordinated:
            if not coordinated:
                return _error(
                    503,
                    "The GPU coordination boundary is busy with an in-flight request",
                    "unavailable_error",
                    "gpu_coordination_busy",
                )
            if game_active():
                return game_block()
            try:
                acquired = application.state.lease_store.acquire(
                    workload, requested_ttl
                )
            except LeaseBusyError:
                return _error(
                    409,
                    "The GPU already has an active non-LLM lease",
                    "conflict_error",
                    "gpu_lease_busy",
                )
            except (LeaseStateError, OSError):
                return _error(
                    503,
                    "The persistent GPU lease marker is unavailable or invalid",
                    "unavailable_error",
                    "gpu_lease_state_invalid",
                )
            except ValueError as error:
                return _error(
                    400,
                    str(error),
                    "invalid_request_error",
                    "gpu_lease_workload_invalid",
                )

            # A clean LLM side is insufficient if an orphaned Comfy or XTTS
            # allocation survived a prior client/container failure. The marker
            # already blocks every new LLM path, so first prove both non-LLM
            # services empty. Failure retains a tokenless, fail-closed marker
            # for the expiry worker to retry.
            residues_cleared = await _cleanup_all_gpu_workloads(
                application.state.comfyui,
                application.state.companion_voice,
            )
            if not residues_cleared:
                try:
                    application.state.lease_store.mark_state(
                        acquired.lease.lease_id, "cleanup_failed"
                    )
                except (LeaseStateError, OSError):
                    pass
                return _error(
                    503,
                    "Prior GPU workload residue could not be cleared; inference remains fail-closed",
                    "unavailable_error",
                    "gpu_residue_cleanup_unconfirmed",
                )

            failure = await _quiesce_router(
                application.state.upstream,
                application.state.catalog,
                LEASE_ROUTER_REQUEST_SECONDS,
            )
            if failure is not None:
                try:
                    application.state.lease_store.abort(acquired.lease.lease_id)
                except (LeaseStateError, OSError):
                    pass
                return failure

            if config.require_native_vllm_ack:
                ack_deadline = time.monotonic() + NATIVE_VLLM_ACK_TIMEOUT_SECONDS
                acknowledged = False
                while time.monotonic() < ack_deadline:
                    if game_active():
                        break
                    try:
                        if application.state.lease_store.acknowledged(
                            acquired.lease.lease_id
                        ):
                            acknowledged = True
                            break
                    except (LeaseStateError, OSError):
                        break
                    await asyncio.sleep(0.1)
                if not acknowledged:
                    try:
                        application.state.lease_store.abort(acquired.lease.lease_id)
                    except (LeaseStateError, OSError):
                        pass
                    if game_active():
                        return game_block()
                    return _error(
                        503,
                        "Native vLLM did not acknowledge GPU quiescence",
                        "unavailable_error",
                        "native_vllm_guard_unavailable",
                    )

            if game_active():
                try:
                    application.state.lease_store.abort(acquired.lease.lease_id)
                except (LeaseStateError, OSError):
                    pass
                return game_block()
            return JSONResponse(
                status_code=201,
                content={
                    "status": "acquired",
                    "lease_token": acquired.token,
                    "ttl_seconds": acquired.lease.ttl_seconds,
                },
            )

    @application.post("/api/v1/gpu/lease/release", response_model=None)
    async def release_gpu_lease(request: Request) -> Response | dict[str, Any]:
        payload = await _json_object(request)
        if isinstance(payload, JSONResponse):
            return payload
        token = payload.get("lease_token")
        if not isinstance(token, str) or not token or len(token) > 256:
            return _error(
                400,
                "lease_token must be a non-empty string",
                "invalid_request_error",
                "gpu_lease_token_invalid",
            )
        async with _bounded_coordination_lock(
            application.state.coordination_lock,
            LEASE_RELEASE_COORDINATION_WAIT_SECONDS,
        ) as coordinated:
            if not coordinated:
                return _error(
                    503,
                    "GPU workload admission is still in flight; the lease remains active",
                    "unavailable_error",
                    "gpu_cleanup_busy",
                )
            try:
                lease = application.state.lease_store.owned_by(token)
            except LeaseTokenError:
                return _error(
                    403,
                    "The release token does not own the active GPU lease",
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
                return {"status": "released", "was_active": False}
            cleaned = await _cleanup_and_complete_lease(
                application.state.lease_store,
                lease,
                application.state.comfyui,
                application.state.companion_voice,
            )
            if not cleaned:
                return _error(
                    503,
                    "GPU workload cleanup could not be confirmed; the lease remains fail-closed",
                    "unavailable_error",
                    "gpu_cleanup_unconfirmed",
                )
        return {"status": "released", "was_active": True}

    @application.api_route(
        "/api/v1/comfy/{endpoint:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy_comfyui(endpoint: str, request: Request) -> Response:
        body = await request.body()
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
        async with application.state.coordination_lock:
            if game_active():
                return game_block()
            try:
                lease = application.state.lease_store.owned_by(token)
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
                response = await application.state.comfyui.request(
                    request.method,
                    f"/{endpoint}",
                    params=request.query_params.multi_items(),
                    headers=headers,
                    content=body,
                    timeout=COMFY_PROXY_REQUEST_SECONDS,
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
        body = await request.body()
        payload: dict[str, Any] | None = None
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
                    model = application.state.catalog.resolve(model_name)
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
        upstream_request = application.state.upstream.build_request(
            request.method,
            f"/v1/{endpoint}",
            params=request.query_params.multi_items(),
            headers=headers,
            content=body,
        )
        wants_stream = payload is not None and payload.get("stream") is True
        coordination_lock: asyncio.Lock = application.state.coordination_lock
        await coordination_lock.acquire()
        if game_active():
            coordination_lock.release()
            return game_block()
        if (blocked := lease_block()) is not None:
            coordination_lock.release()
            return blocked
        try:
            if wants_stream:
                upstream_response = await application.state.upstream.send(
                    upstream_request, stream=True
                )
            else:
                upstream_response = await application.state.upstream.send(
                    upstream_request
                )
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
            content = await upstream_response.aread()
            await upstream_response.aclose()
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
                await upstream_response.aclose()
                coordination_lock.release()

        return StreamingResponse(
            relay(),
            status_code=upstream_response.status_code,
            headers=_response_headers(upstream_response),
            media_type=None,
        )

    return application
