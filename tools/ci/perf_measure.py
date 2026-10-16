"""Authenticated public-contract measurements against the owned loopback proxy."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from time import perf_counter_ns
from typing import Any

import httpx
import redis.asyncio as redis

from .perf_schema import TARGETS
from .perf_seed import OWNER, reset_rate_limits


def _archive_contract(body: Any) -> bool:
    return (
        isinstance(body, list)
        and len(body) == 20
        and all(
            isinstance(row, dict)
            and str(row.get("annotation", "")).startswith(f"seed:{OWNER}:")
            for row in body
        )
    )


def _audio_contract(body: Any) -> bool:
    if not isinstance(body, dict) or body.get("status") != "completed":
        return False
    try:
        return base64.b64decode(body.get("audio", ""), validate=True).startswith(
            b"RIFF"
        )
    except (ValueError, TypeError):
        return False


def _messages_contract(body: Any) -> bool:
    rows = body.get("messages") if isinstance(body, dict) else None
    return (
        isinstance(rows, list)
        and len(rows) == 50
        and all(
            isinstance(row, dict)
            and str(row.get("content", "")).startswith(f"seed:{OWNER}:")
            for row in rows
        )
    )


def contract(route: str, body: Any) -> bool:
    if route in {"/health", "/api/health/ready", "/api/health/live"}:
        return isinstance(body, dict) and body.get("status") in {"ok", "healthy"}
    if route == "/api/auth/login":
        return (
            isinstance(body, dict)
            and body.get("user_id") == OWNER
            and isinstance(body.get("token"), str)
            and bool(body["token"])
        )
    if route == "/api/auth/verify":
        return bool(body == {"user_id": OWNER})
    if route == "/api/affection":
        return isinstance(body, dict) and isinstance(body.get("level"), int)
    if route == "/api/messages":
        return _messages_contract(body)
    if route == "/api/memories":
        return _archive_contract(body)
    if route == "/api/tts":
        return (
            isinstance(body, dict)
            and bool(body.get("job_id"))
            and body.get("status") in {"queued", "running"}
        )
    if route == "/api/tts/jobs/{job_id}":
        return _audio_contract(body)
    return False


class Sampler:
    def __init__(
        self,
        client: httpx.AsyncClient,
        cache: redis.Redis,
        password: str,
        temperature_check: Callable[[], None],
    ):
        self.client, self.cache = client, cache
        self.password, self.temperature_check = password, temperature_check
        self.headers: dict[str, str] = {}
        self.job_id = ""

    async def initialize(self) -> None:
        deadline = asyncio.get_running_loop().time() + 90
        while True:
            try:
                response = await self.client.get("/api/health/ready")
                if response.status_code == 200 and contract(
                    "/api/health/ready", response.json()
                ):
                    break
            except (httpx.HTTPError, ValueError):
                pass
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError("Actual core readiness never became healthy")
            await asyncio.sleep(0.25)
        await reset_rate_limits(self.cache)
        login = await self.client.post(
            "/api/auth/login", json={"username": OWNER, "password": self.password}
        )
        if login.status_code != 200 or not contract("/api/auth/login", login.json()):
            raise RuntimeError("Actual seeded login failed")
        self.headers = {"Authorization": "Bearer " + login.json()["token"]}
        for route in (
            "/api/auth/verify",
            "/api/messages",
            "/api/memories",
            "/api/affection",
        ):
            response = await self.client.get(route)
            if response.status_code != 401:
                raise RuntimeError("Unauthenticated protected route accepted")

    async def complete_job(self) -> None:
        deadline = asyncio.get_running_loop().time() + 15
        while True:
            await reset_rate_limits(self.cache)
            response = await self.client.get(
                f"/api/tts/jobs/{self.job_id}", headers=self.headers
            )
            if response.status_code != 200:
                raise RuntimeError("Accepted voice job disappeared")
            body = response.json()
            if contract("/api/tts/jobs/{job_id}", body):
                return
            if (
                body.get("status") in {"failed", "cancelled"}
                or asyncio.get_running_loop().time() > deadline
            ):
                raise RuntimeError(
                    "Accepted job did not complete through deterministic fixture"
                )
            await asyncio.sleep(0.01)

    async def one(self, method: str, route: str, expected: int) -> dict[str, Any]:
        await reset_rate_limits(self.cache)  # outside the timed public HTTP operation
        self.temperature_check()
        path = route.replace("{job_id}", self.job_id)
        body = (
            {"username": OWNER, "password": self.password}
            if route == "/api/auth/login"
            else {"text": "Isolated fixture speech.", "language": "en"}
            if route == "/api/tts"
            else None
        )
        start = perf_counter_ns()
        status = 599
        value: Any = None
        try:
            response = await self.client.request(
                method,
                path,
                headers={} if route == "/api/auth/login" else self.headers,
                json=body,
            )
            status = response.status_code
            value = response.json()
        except (httpx.HTTPError, ValueError):
            pass
        elapsed = (perf_counter_ns() - start) / 1e6
        valid = status == expected and contract(route, value)
        if valid and route == "/api/tts":
            self.job_id = value["job_id"]
            await self.complete_job()  # admission timer excludes fixture synthesis
        return {"elapsed_ms": elapsed, "status": status, "contract_ok": valid}

    async def measure(
        self, warmup: int, samples: int, concurrency: int
    ) -> list[dict[str, Any]]:
        result = []
        for method, route, expected in TARGETS:
            for _ in range(warmup):
                if not (await self.one(method, route, expected))["contract_ok"]:
                    raise RuntimeError(
                        f"Warmup public contract failed: {method} {route}"
                    )
            # Sequential admission avoids measuring deliberate queue overload. Other
            # routes use bounded batches when the caller explicitly requests load.
            rows: list[dict[str, Any]] = []
            width = 1 if route == "/api/tts" else concurrency
            while len(rows) < samples:
                rows.extend(
                    await asyncio.gather(
                        *(
                            self.one(method, route, expected)
                            for _ in range(min(width, samples - len(rows)))
                        )
                    )
                )
            result.append(
                {
                    "method": method,
                    "route": route,
                    "warmup_count": warmup,
                    "samples": rows,
                }
            )
        return result
