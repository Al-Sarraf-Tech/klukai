"""Mandatory explicit disposable-backend gate (not part of unit collection).

Run KLUKAI_RUNTIME_BACKENDS=1 python -m pytest tests/runtime_backends.py -q.
Missing Docker, cached pinned images or evidence output fails this gate.
Only the containers created here may be removed. No production endpoints,
credentials, directories, provider accounts or GPU devices are used.
"""

from __future__ import annotations

from builtins import ExceptionGroup
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
from typing import Any

import httpx
import pytest
import redis.asyncio as redis
from psycopg_pool import AsyncConnectionPool

from app import db, memory as memory_module, memory_session, metrics
from app.circuit_breakers import all_breakers, get_breaker
from app.memory import MemoryManager
from app.observability import health_cache

IMAGES = {
    "postgres": "sha256:650b442ec4f71f47b92505de963bbc4054f8c7df0df30956aef30e27d7e4dea4",
    "redis": "sha256:e0cd36901ed62fccffb8186d099492a327df98abbe2269ffe0bd2b1b1d6732fb",
    "qdrant": "sha256:d88816626f62fbf8d3468cd8b6330ba1bff6d2219b05c00d9e1b85311747ac2a",
}
ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATHS = [
    "app/db.py",
    "app/memory.py",
    "app/memory_session.py",
    "app/dependencies.py",
    "app/circuit_breakers.py",
    "app/observability/health_cache.py",
    "tests/runtime_backends.py",
    "tests/runtime_backend_support.py",
    "app/auth.py",
]


def docker(*args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["docker", *args], text=True, capture_output=True, timeout=45, env=env
    )
    if result.returncode:
        raise RuntimeError(f"Docker {args[0]} failed: {result.stderr[:500]}")
    return (
        (result.stdout + result.stderr).strip()
        if args[0] == "logs"
        else result.stdout.strip()
    )


@dataclass(frozen=True)
class Backends:
    ports: dict[str, int]
    hosts: dict[str, str]
    password: str
    evidence: Path


@pytest.fixture(scope="module")
def backends(tmp_path_factory: pytest.TempPathFactory):
    if os.environ.get("KLUKAI_RUNTIME_BACKENDS") != "1":
        pytest.fail("Explicit disposable gate requires KLUKAI_RUNTIME_BACKENDS=1")
    output = os.environ.get("KLUKAI_RUNTIME_EVIDENCE")
    if not output:
        pytest.fail("KLUKAI_RUNTIME_EVIDENCE must name a private evidence directory")
    evidence = Path(output)
    evidence.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(5)
    network = f"klukai-s-runtime-{suffix}"
    names: list[str] = []
    password = secrets.token_urlsafe(24)
    ports: dict[str, int] = {}
    hosts: dict[str, str] = {}
    containers: dict[str, Any] = {}
    docker("network", "create", "--internal", network)
    try:
        for dependency, port, cpus, memory in (
            ("postgres", 5432, ".7", "384m"),
            ("redis", 6379, ".3", "96m"),
            ("qdrant", 6333, ".8", "384m"),
        ):
            image = os.environ.get(
                f"KLUKAI_RUNTIME_{dependency.upper()}_IMAGE", IMAGES[dependency]
            )
            if not image.startswith("sha256:") or len(image) != 71:
                raise ValueError(
                    "Runtime test images require a full immutable image ID"
                )
            assert docker("image", "inspect", "--format", "{{.Id}}", image) == image
            name = f"{network}-{dependency}"
            args = [
                "run",
                "-d",
                "--pull=never",
                "--name",
                name,
                "--network",
                network,
                "--cpus",
                cpus,
                "--memory",
                memory,
                "--memory-swap",
                memory,
                "--pids-limit",
                "128",
                "--security-opt",
                "no-new-privileges",
            ]
            env = None
            if dependency == "postgres":
                env = {**os.environ, "POSTGRES_PASSWORD": password}
                args += [
                    "--env",
                    "POSTGRES_PASSWORD",
                    "--env",
                    "POSTGRES_USER=runtime_test",
                    "--env",
                    "POSTGRES_DB=runtime_test",
                    "--tmpfs",
                    "/var/lib/postgresql/data:rw,size=256m",
                ]
            elif dependency == "qdrant":
                args += [
                    "--tmpfs",
                    "/qdrant/storage:rw,size=128m",
                    "--env",
                    "QDRANT__TELEMETRY_DISABLED=true",
                ]
            args.append(image)
            if dependency == "redis":
                args += ["valkey-server", "--save", "", "--appendonly", "no"]
            container_id = docker(*args, env=env)
            names.append(name)
            inspect = json.loads(docker("inspect", name))[0]
            # Docker29 intentionally does not publish internal-network ports.
            # Only the fault proxies expose random loopback listener ports.
            assert not inspect["HostConfig"]["PortBindings"]
            hosts[dependency] = inspect["NetworkSettings"]["Networks"][network][
                "IPAddress"
            ]
            ports[dependency] = port
            containers[dependency] = {
                "id": container_id,
                "image": image,
                "cpus": cpus,
                "memory": memory,
                "host": hosts[dependency],
                "port": ports[dependency],
            }
        deadline = time.monotonic() + 35
        for dependency, port in ports.items():
            while True:
                try:
                    with socket.create_connection(
                        (hosts[dependency], port), timeout=0.2
                    ):
                        break
                except OSError:
                    if time.monotonic() > deadline:
                        raise RuntimeError(
                            f"Disposable {dependency} did not start"
                        ) from None
                    time.sleep(0.1)
        (evidence / "containers.json").write_text(
            json.dumps(containers, indent=2) + "\n"
        )
        yield Backends(ports, hosts, password, evidence)
    finally:
        cleanup_errors = []
        for name in names:
            try:
                (evidence / f"{name}.log").write_text(docker("logs", name))
            except RuntimeError as error:
                cleanup_errors.append(str(error))
            try:
                docker("rm", "-fv", name)
            except RuntimeError as error:
                cleanup_errors.append(str(error))
        try:
            docker("network", "rm", network)
        except RuntimeError as error:
            cleanup_errors.append(str(error))
        (evidence / "cleanup.json").write_text(
            json.dumps(
                {
                    "network": network,
                    "containers_removed": names,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "errors": cleanup_errors,
                }
            )
            + "\n"
        )
        assert not cleanup_errors, cleanup_errors


class FaultProxy:
    """Cut established TCP sessions without changing backend data or state."""

    def __init__(self, host: str, port: int):
        self.backend_host = host
        self.backend_port = port
        self.available = True
        self.accepted = 0
        self.writers: set[asyncio.StreamWriter] = set()
        self.tasks: set[asyncio.Task[None]] = set()
        self.server: asyncio.Server | None = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        return int(self.server.sockets[0].getsockname()[1])

    def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.create_task(self._relay(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _relay(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.accepted += 1
        self.writers.add(writer)
        upstream: asyncio.StreamWriter | None = None
        try:
            if not self.available:
                return
            remote, upstream = await asyncio.open_connection(
                self.backend_host, self.backend_port
            )
            self.writers.add(upstream)

            async def copy(
                source: asyncio.StreamReader, target: asyncio.StreamWriter
            ) -> None:
                while data := await source.read(65536):
                    target.write(data)
                    await target.drain()

            async with asyncio.TaskGroup() as group:
                left = group.create_task(copy(reader, upstream))
                right = group.create_task(copy(remote, writer))
                await asyncio.wait((left, right), return_when=asyncio.FIRST_COMPLETED)
                left.cancel()
                right.cancel()
        except (OSError, ExceptionGroup):
            pass  # The injected disconnect is the controlled transport fault.
        finally:
            for stream in (writer, upstream):
                if stream is not None:
                    stream.close()
                    self.writers.discard(stream)
                    try:
                        await stream.wait_closed()
                    except OSError:
                        pass

    async def cut(self) -> None:
        self.available = False
        for writer in tuple(self.writers):
            writer.close()
        await asyncio.sleep(0.02)

    async def close(self) -> None:
        await self.cut()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        assert not self.tasks and not self.writers


@pytest.fixture
async def clients(
    backends: Backends, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
):
    proxies = {
        dep: FaultProxy(backends.hosts[dep], port)
        for dep, port in backends.ports.items()
    }
    ports = {dep: await proxy.start() for dep, proxy in proxies.items()}
    dsn = f"postgresql://runtime_test:{backends.password}@127.0.0.1:{ports['postgres']}/runtime_test"
    pool = db.ResilientConnectionPool(
        dsn,
        min_size=0,
        max_size=2,
        open=False,
        timeout=0.3,
        reconnect_timeout=0.2,
        check=AsyncConnectionPool.check_connection,
    )
    await pool.open()
    monkeypatch.setattr(db, "_pool", pool)
    monkeypatch.setattr(db, "MAX_RETRIES", 0)
    redis_url = f"redis://127.0.0.1:{ports['redis']}/0"
    monkeypatch.setattr(memory_module, "REDIS_URL", redis_url)
    monkeypatch.setattr(memory_session, "REDIS_URL", redis_url)
    monkeypatch.setattr(
        memory_module, "QDRANT_URL", f"http://127.0.0.1:{ports['qdrant']}"
    )
    monkeypatch.setattr(health_cache, "PING_TIMEOUT_SECONDS", 0.4)
    memory = MemoryManager()
    memory._redis = redis.Redis.from_url(
        redis_url, decode_responses=True, socket_timeout=0.3, socket_connect_timeout=0.3
    )
    memory._http = httpx.AsyncClient(timeout=0.4, trust_env=False)
    for name in ("postgres", "redis", "qdrant"):
        get_breaker(name).cfg.error_threshold = 1
    health_cache.clear_cache()
    try:
        deadline = time.monotonic() + 25
        while (await db.check_health())["status"] != "ok":
            get_breaker("postgres").force_close()
            if time.monotonic() > deadline:
                raise AssertionError("Disposable PG protocol did not become healthy")
            await asyncio.sleep(0.1)
        assert await memory._redis.ping()
        yield memory, proxies
    finally:
        await memory.close()
        await pool.close()
        for proxy in proxies.values():
            await proxy.close()
        health_cache.clear_cache()
        assert pool.closed and memory._http.is_closed
        (backends.evidence / f"{request.node.name}-cleanup.json").write_text(
            json.dumps(
                {
                    "pool_closed": pool.closed,
                    "http_closed": memory._http.is_closed,
                    "proxy_tasks": sum(len(proxy.tasks) for proxy in proxies.values()),
                    "proxy_streams": sum(
                        len(proxy.writers) for proxy in proxies.values()
                    ),
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )
            + "\n"
        )


def evidence(
    backends: Backends, dependency: str, events: list[dict[str, object]]
) -> None:
    hashes = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in SOURCE_PATHS
    }
    document = {
        "dependency": dependency,
        "timestamp": datetime.now(UTC).isoformat(),
        "source_sha256": hashes,
        "states": events,
        "emitted_transitions": {
            key: count
            for key, count in metrics.snapshot()["counters"].items()
            if key.startswith("circuit_transitions_total{")
            and f"dep={dependency}," in key
        },
        "breakers": {
            name: breaker.state.value for name, breaker in all_breakers().items()
        },
    }
    (backends.evidence / f"{dependency}.json").write_text(
        json.dumps(document, indent=2) + "\n"
    )
