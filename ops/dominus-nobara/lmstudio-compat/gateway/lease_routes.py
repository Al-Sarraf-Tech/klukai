"""GPU lease acquisition and release with bounded coordination."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from .gpu_lease import (
    LEASE_DEFAULT_SECONDS,
    LeaseBusyError,
    LeaseStateError,
    LeaseTokenError,
)
from .http_policy import _error, _json_object, _bounded_coordination_lock
from .model_state import _quiesce_router
from .runtime import GatewayRuntime
from .workload_cleanup import _cleanup_all_gpu_workloads, _cleanup_and_complete_lease


def _acquisition_request(payload: dict[str, Any]) -> tuple[str, int] | JSONResponse:
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

    return workload, requested_ttl


async def _wait_native_ack(runtime: GatewayRuntime, lease_id: str) -> bool:
    ack_deadline = time.monotonic() + runtime.limits().NATIVE_VLLM_ACK_TIMEOUT_SECONDS
    acknowledged = False
    while time.monotonic() < ack_deadline:
        if runtime.game_active():
            break
        try:
            if runtime.lease_store.acknowledged(lease_id):
                acknowledged = True
                break
        except (LeaseStateError, OSError):
            break
        await asyncio.sleep(0.1)

    return acknowledged


def register_lease_routes(application: FastAPI, runtime: GatewayRuntime) -> None:
    @application.post("/api/v1/gpu/lease/acquire", response_model=None)
    async def acquire_gpu_lease(request: Request) -> Response | dict[str, Any]:
        payload = await _json_object(request, runtime.limits().MAX_REQUEST_BODY_BYTES)
        if isinstance(payload, JSONResponse):
            return payload
        validated = _acquisition_request(payload)
        if isinstance(validated, JSONResponse):
            return validated
        workload, requested_ttl = validated

        async with _bounded_coordination_lock(
            runtime.coordination_lock,
            runtime.limits().LEASE_ACQUIRE_COORDINATION_WAIT_SECONDS,
        ) as coordinated:
            if not coordinated:
                return _error(
                    503,
                    "The GPU coordination boundary is busy with an in-flight request",
                    "unavailable_error",
                    "gpu_coordination_busy",
                )
            if runtime.game_active():
                return runtime.game_block()
            try:
                acquired = runtime.lease_store.acquire(workload, requested_ttl)
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
                runtime.limits(),
                runtime.comfyui,
                runtime.companion_voice,
            )
            if not residues_cleared:
                try:
                    runtime.lease_store.mark_state(
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
                runtime.limits(),
                runtime.upstream,
                runtime.catalog,
                runtime.limits().LEASE_ROUTER_REQUEST_SECONDS,
            )
            if failure is not None:
                try:
                    runtime.lease_store.abort(acquired.lease.lease_id)
                except (LeaseStateError, OSError):
                    pass
                return failure

            if runtime.config.require_native_vllm_ack:
                acknowledged = await _wait_native_ack(runtime, acquired.lease.lease_id)
                if not acknowledged:
                    try:
                        runtime.lease_store.abort(acquired.lease.lease_id)
                    except (LeaseStateError, OSError):
                        pass
                    if runtime.game_active():
                        return runtime.game_block()
                    return _error(
                        503,
                        "Native vLLM did not acknowledge GPU quiescence",
                        "unavailable_error",
                        "native_vllm_guard_unavailable",
                    )

            if runtime.game_active():
                try:
                    runtime.lease_store.abort(acquired.lease.lease_id)
                except (LeaseStateError, OSError):
                    pass
                return runtime.game_block()
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
        payload = await _json_object(request, runtime.limits().MAX_REQUEST_BODY_BYTES)
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
            runtime.coordination_lock,
            runtime.limits().LEASE_RELEASE_COORDINATION_WAIT_SECONDS,
        ) as coordinated:
            if not coordinated:
                return _error(
                    503,
                    "GPU workload admission is still in flight; the lease remains active",
                    "unavailable_error",
                    "gpu_cleanup_busy",
                )
            try:
                lease = runtime.lease_store.owned_by(token)
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
                runtime.limits(),
                runtime.lease_store,
                lease,
                runtime.comfyui,
                runtime.companion_voice,
            )
            if not cleaned:
                return _error(
                    503,
                    "GPU workload cleanup could not be confirmed; the lease remains fail-closed",
                    "unavailable_error",
                    "gpu_cleanup_unconfirmed",
                )
        return {"status": "released", "was_active": True}
