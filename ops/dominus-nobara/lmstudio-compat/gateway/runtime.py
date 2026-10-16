"""Typed shared services, GPU admission policy and lease-expiry ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
import time
from typing import Any

import httpx
from fastapi.responses import JSONResponse
from .catalog import Catalog, Model
from .gpu_lease import LeaseStore, LeaseStateError
from .http_policy import _error
from .limits import Limits
from .model_state import _router_states
from .settings import Settings
from .workload_cleanup import _cleanup_and_complete_lease

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GatewayServices:
    catalog: Catalog
    upstream: httpx.AsyncClient
    comfyui: httpx.AsyncClient
    companion_voice: httpx.AsyncClient
    coordination_lock: asyncio.Lock
    lease_store: LeaseStore


@dataclass(slots=True)
class GatewayRuntime:
    config: Settings
    limits: Callable[[], Limits]
    _services: GatewayServices | None = None

    @property
    def services(self) -> GatewayServices:
        if self._services is None:
            raise RuntimeError("gateway services are not initialized")
        return self._services

    @property
    def catalog(self) -> Catalog:
        return self.services.catalog

    @property
    def upstream(self) -> httpx.AsyncClient:
        return self.services.upstream

    @property
    def comfyui(self) -> httpx.AsyncClient:
        return self.services.comfyui

    @property
    def companion_voice(self) -> httpx.AsyncClient:
        return self.services.companion_voice

    @property
    def coordination_lock(self) -> asyncio.Lock:
        return self.services.coordination_lock

    @property
    def lease_store(self) -> LeaseStore:
        return self.services.lease_store

    def game_active(self, model: Model | None = None) -> bool:
        # A designated gaming-safe model (small/CPU-offloaded) is allowed to
        # keep serving during the graceful degrade instead of the blanket
        # block -- everything else (other LLMs, ComfyUI, voice/GPU leases)
        # stays hard-blocked. No model given (non-LLM call sites) blocks
        # unconditionally, same as before this patch.
        if not self.config.game_marker_path.exists():
            return False
        if model is not None and model.router_id in self.config.gaming_safe_model_ids:
            return False
        return True

    def game_block(self) -> JSONResponse:
        return _error(
            503,
            "Inference and model loading are disabled while a game is active",
            "unavailable_error",
            "game_active",
        )

    def lease_block(self) -> JSONResponse | None:
        try:
            lease = self.lease_store.active()
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

    async def catalog_states(self) -> tuple[Catalog, dict[str, str]]:
        catalog: Catalog = self.catalog
        states, _ = await _router_states(
            self.upstream, catalog, self.config.health_timeout_seconds
        )
        return catalog, states

    def requested_model(
        self, payload: dict[str, Any], field: str
    ) -> tuple[Model, str] | JSONResponse:
        identifier = payload.get(field)
        if not isinstance(identifier, str) or not identifier:
            return _error(
                400,
                f"{field} must be a non-empty string",
                "invalid_request_error",
                400,
            )
        catalog: Catalog = self.catalog
        model = catalog.resolve(identifier)
        if model is None:
            return _error(
                404,
                f"Unknown model: {identifier}",
                "invalid_request_error",
                "model_not_found",
            )
        return model, identifier

    async def expire_stale_lease(self) -> None:
        last_attempted_lease_id: str | None = None
        last_attempt_monotonic = 0.0
        while True:
            async with self.coordination_lock:
                try:
                    lease = self.lease_store.active()
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
                        >= self.limits().LEASE_CLEANUP_RETRY_SECONDS
                    )
                    if retry_due:
                        last_attempted_lease_id = lease.lease_id
                        last_attempt_monotonic = now
                        cleaned = await _cleanup_and_complete_lease(
                            self.limits(),
                            self.lease_store,
                            lease,
                            self.comfyui,
                            self.companion_voice,
                        )
                        if not cleaned:
                            logger.error(
                                "GPU lease cleanup remains fail-closed for %s",
                                lease.workload,
                            )
            await asyncio.sleep(self.limits().LEASE_EXPIRY_POLL_SECONDS)
