"""LM Studio API facade over a llama.cpp multi-model router."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
import httpx

from .catalog import Catalog
from .clients import _default_client, _default_comfy_client, _default_voice_client
from .gpu_lease import LeaseStore, LeaseStateError
from .http_policy import _authorized, _bypass_allowed, _error
from .lease_routes import register_lease_routes
from .limits import Limits
from .model_routes import register_model_routes
from .proxy_routes import register_proxy_routes
from .runtime import GatewayRuntime, GatewayServices
from .settings import Settings

ClientFactory = Callable[[Settings], httpx.AsyncClient]


# The client-visible ceiling is 300 seconds (5 min; lowered 2026-08-17 from
# 900s to unload big presets faster). The pinned router is launched at 298
# seconds because its roughly one-second scheduler needs a safety margin to
# guarantee actual model residency never exceeds this policy ceiling.
MODEL_IDLE_TTL_SECONDS = 300
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
# /v1/* holds the coordination lock from admission until the relayed body is
# fully consumed, so a client that stops reading (or a half-open peer) would
# otherwise queue every later inference request behind it indefinitely while
# /health stays green. Later requests wait this long, then fail fast with a
# retry hint. The upstream read timeout is a separate, longer budget.
INFERENCE_COORDINATION_WAIT_SECONDS = 120.0
# Request bodies are buffered in gateway memory before forwarding. This caps
# what one client can make the gateway hold; the compose mem_limit is the
# backstop behind it.
MAX_REQUEST_BODY_BYTES = 64 * 1024 * 1024


def _limits() -> Limits:
    return Limits(
        MODEL_IDLE_TTL_SECONDS=MODEL_IDLE_TTL_SECONDS,
        NATIVE_VLLM_ACK_TIMEOUT_SECONDS=NATIVE_VLLM_ACK_TIMEOUT_SECONDS,
        LEASE_ACQUIRE_COORDINATION_WAIT_SECONDS=LEASE_ACQUIRE_COORDINATION_WAIT_SECONDS,
        LEASE_RELEASE_COORDINATION_WAIT_SECONDS=LEASE_RELEASE_COORDINATION_WAIT_SECONDS,
        LEASE_ROUTER_REQUEST_SECONDS=LEASE_ROUTER_REQUEST_SECONDS,
        LEASE_ROUTER_QUIESCE_SECONDS=LEASE_ROUTER_QUIESCE_SECONDS,
        ROUTER_UNLOAD_DRAIN_SECONDS=ROUTER_UNLOAD_DRAIN_SECONDS,
        ROUTER_UNLOAD_POLL_SECONDS=ROUTER_UNLOAD_POLL_SECONDS,
        LEASE_EXPIRY_POLL_SECONDS=LEASE_EXPIRY_POLL_SECONDS,
        LEASE_CLEANUP_RETRY_SECONDS=LEASE_CLEANUP_RETRY_SECONDS,
        COMFY_QUEUE_DRAIN_SECONDS=COMFY_QUEUE_DRAIN_SECONDS,
        COMFY_QUEUE_POLL_SECONDS=COMFY_QUEUE_POLL_SECONDS,
        COMFY_CLEANUP_REQUEST_SECONDS=COMFY_CLEANUP_REQUEST_SECONDS,
        VOICE_CLEANUP_REQUEST_SECONDS=VOICE_CLEANUP_REQUEST_SECONDS,
        GPU_WORKLOAD_CLEANUP_SECONDS=GPU_WORKLOAD_CLEANUP_SECONDS,
        COMFY_PROXY_REQUEST_SECONDS=COMFY_PROXY_REQUEST_SECONDS,
        MAX_RESIDENT_ROUTER_MODELS=MAX_RESIDENT_ROUTER_MODELS,
        INFERENCE_COORDINATION_WAIT_SECONDS=INFERENCE_COORDINATION_WAIT_SECONDS,
        MAX_REQUEST_BODY_BYTES=MAX_REQUEST_BODY_BYTES,
    )


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
    runtime = GatewayRuntime(config, _limits)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        services = GatewayServices(
            catalog=Catalog.from_path(config.catalog_path),
            upstream=make_client(config),
            comfyui=make_comfy_client(config),
            companion_voice=make_voice_client(config),
            coordination_lock=asyncio.Lock(),
            lease_store=LeaseStore(
                config.gpu_lease_marker_path, config.gpu_lease_ack_path
            ),
        )
        runtime._services = services
        # Retain the established ASGI inspection surface; routes use the typed owner.
        application.state.catalog = services.catalog
        application.state.upstream = services.upstream
        application.state.comfyui = services.comfyui
        application.state.companion_voice = services.companion_voice
        application.state.coordination_lock = services.coordination_lock
        application.state.lease_store = services.lease_store
        expiry_task = asyncio.create_task(runtime.expire_stale_lease())
        try:
            yield
        finally:
            expiry_task.cancel()
            with suppress(asyncio.CancelledError):
                await expiry_task
            await services.companion_voice.aclose()
            await services.comfyui.aclose()
            await services.upstream.aclose()

    application = FastAPI(
        title="Dominus LM Studio compatibility gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def bearer_authentication(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if (
            request.url.path == "/health"
            or _bypass_allowed(request)
            or _authorized(request, config.gateway_token)
        ):
            return await call_next(request)
        response = _error(
            401,
            "Missing or invalid bearer token",
            "authentication_error",
            401,
        )
        response.headers["WWW-Authenticate"] = "Bearer"
        return response

    @application.get("/health")
    async def health() -> JSONResponse:
        upstream_status = "unavailable"
        comfyui_status = "unavailable"
        try:
            response = await runtime.upstream.get(
                "/health", timeout=runtime.config.health_timeout_seconds
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
            comfy_response = await runtime.comfyui.get(
                "/system_stats", timeout=runtime.config.health_timeout_seconds
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
            lease = runtime.lease_store.active()
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
                "models": len(runtime.catalog.models),
                "upstream": upstream_status,
                "comfyui_status": comfyui_status,
                "game_active": runtime.game_active(),
                "gpu_lease_state": lease_state,
                "gpu_lease_active": lease_state != "inactive",
                "gpu_lease_workload": lease_workload,
                "gpu_lease_remaining_seconds": lease_remaining_seconds,
                "model_idle_ttl_seconds": runtime.limits().MODEL_IDLE_TTL_SECONDS,
                # True while /v1/* (or a lease operation) holds the
                # coordination lock. A long-lived True with a green upstream
                # is the "stalled client wedged inference" signature.
                "inference_busy": runtime.coordination_lock.locked(),
            }
        )

    register_model_routes(application, runtime)
    register_lease_routes(application, runtime)
    register_proxy_routes(application, runtime)
    return application
