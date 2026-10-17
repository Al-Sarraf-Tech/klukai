"""Bounded local Docker resources for reusable disposable CI stacks.

No bind mounts, host networking, sockets, production endpoints or inherited
container environment are accepted. Every image input is immutable and cached.
"""

from __future__ import annotations
import json
import os
import re
from pathlib import Path
import secrets
import socket
import subprocess
import time
from typing import Any

from .perf_gate import image_reference
from .perf_recording import (
    ACTIVE,
    CONTAINER_VIEW,
    IMAGE_VIEW,
    LABEL,
    NETWORK_VIEW,
    VOLUME_VIEW,
    base_environment,
)

BACKEND_IMAGES = {
    "postgres": "sha256:650b442ec4f71f47b92505de963bbc4054f8c7df0df30956aef30e27d7e4dea4",
    "redis": "sha256:e0cd36901ed62fccffb8186d099492a327df98abbe2269ffe0bd2b1b1d6732fb",
    "qdrant": "sha256:d88816626f62fbf8d3468cd8b6330ba1bff6d2219b05c00d9e1b85311747ac2a",
    "fixture": "sha256:18e30fdc01cec4f4e299dff7e93c1acc385807403c6bcb1ffdfe704d2bf64202",
}
RESOURCES = {
    "core": {"nano_cpus": 1_400_000_000, "memory_bytes": 768 * 1024**2},
    "postgres": {"nano_cpus": 400_000_000, "memory_bytes": 384 * 1024**2},
    "redis": {"nano_cpus": 100_000_000, "memory_bytes": 96 * 1024**2},
    "qdrant": {"nano_cpus": 400_000_000, "memory_bytes": 384 * 1024**2},
    "fixture": {"nano_cpus": 200_000_000, "memory_bytes": 96 * 1024**2},
}


def docker(
    *arguments: str,
    environment: dict[str, str] | None = None,
    stdin: str | None = None,
    check: bool = True,
) -> str:
    recorder = ACTIVE.get()
    if recorder is not None:
        return recorder.run(
            *arguments, environment=environment, stdin=stdin, check=check
        )
    result = subprocess.run(
        ["docker", *arguments],
        input=stdin,
        text=True,
        capture_output=True,
        timeout=50,
        env={**os.environ, **(environment or {})},
    )
    if check and result.returncode:
        raise RuntimeError(f"Docker {arguments[0]} failed: {result.stderr[:500]}")
    return (
        result.stdout + result.stderr
        if arguments[0] == "logs"
        else result.stdout.strip()
    )


def inspect_image(reference: str, *, role: str = "core") -> dict[str, Any]:
    image_reference(reference)
    data = json.loads(docker("image", "inspect", "--format=" + IMAGE_VIEW, reference))
    if isinstance(data, list):
        data = data[0]
    for item in data.get("Config", {}).get("Env", []):
        if isinstance(
            item, str
        ):  # Legacy test boundary; actual projection emits objects.
            key, _, value = item.partition("=")
        else:
            key, value = item["key"], item.get("value", item.get("nonempty"))
        sensitive = any(
            marker in key.upper()
            for marker in ("API_KEY", "TOKEN", "PASSWORD", "SECRET", "CREDENTIAL")
        ) or key in {"DATABASE_URL", "REDIS_URL"}
        if value and sensitive:
            raise ValueError(
                "Image contains baked credentials; use a credential-free input"
            )
    base_environment(data["Config"], role)
    return {
        "id": data["Id"],
        "revision": data.get("Config", {}).get("Revision"),
        "os": data["Os"],
        "architecture": data["Architecture"],
    }


def cpu_temperature() -> float:
    readings: list[float] = []
    for directory in Path("/sys/class/hwmon").glob("*"):
        try:
            name = (directory / "name").read_text().strip()
        except OSError:
            continue
        if name in {"coretemp", "k10temp", "cpu_thermal"}:
            readings.extend(
                float(file.read_text()) / 1000 for file in directory.glob("temp*_input")
            )
    if not readings or not 0 < max(readings) < 70:
        raise RuntimeError("CPU thermal capability missing or70C safety limit reached")
    return max(readings)


def verify_absent(kind: str, name: str) -> None:
    view = {
        "container": CONTAINER_VIEW,
        "network": NETWORK_VIEW,
        "volume": VOLUME_VIEW,
    }[kind]
    arguments = ["inspect"] if kind == "container" else [kind, "inspect"]
    docker(*arguments, "--format=" + view, name, check=False)
    recorder = ACTIVE.get()
    if recorder is None:
        return  # Noncertifying legacy callers have no retained command exit proof.
    row = recorder.rows[-1]
    error = (recorder.output / row["stderr"]).read_text()
    if row["exit_code"] == 0 or not re.search(
        r"(?:No such (?:object|container|network|volume): "
        + re.escape(name)
        + r"|(?:network|volume) "
        + re.escape(name)
        + r" not found|get "
        + re.escape(name)
        + r": no such volume)(?:\s|$)",
        error,
    ):
        raise RuntimeError("Disposable resource absence unproven")


class DisposableStack:
    """Own named containers/network/volume; cleanup errors are gate failures."""

    def __init__(self, output: Path, repository: Path):
        self.output, self.repository = output, repository
        self.prefix = "klukai-s-perf-" + secrets.token_hex(5)
        self.network = self.prefix
        self.volume = self.prefix + "-fixtures"
        self.containers: list[str] = []
        self.backends: dict[str, dict[str, Any]] = {}
        self.backend_images = dict(BACKEND_IMAGES)
        self.backend_inputs = dict(BACKEND_IMAGES)
        self.password = secrets.token_urlsafe(24)
        if ACTIVE.get() is not None:
            ACTIVE.get().secrets.append(self.password)
        self.maximum_temperature = cpu_temperature()
        self.cleanup_verified = False
        self.network_created = False
        self.volume_created = False

    def _create(
        self,
        dependency: str,
        image: str,
        *,
        env: dict[str, str] | None = None,
        arguments: list[str] | None = None,
        suffix: str | None = None,
        entrypoint: str | None = None,
        fixture_volume: bool = False,
    ) -> str:
        resource = RESOURCES[dependency]
        name = self.prefix + "-" + (suffix or dependency)
        options = [
            "create",
            "--pull=never",
            "--name",
            name,
            "--label",
            LABEL + "=" + self.prefix,
            "--network",
            self.network,
            "--network-alias",
            dependency,
            "--cpus",
            str(resource["nano_cpus"] / 1e9),
            "--memory",
            str(resource["memory_bytes"]),
            "--memory-swap",
            str(resource["memory_bytes"]),
            "--pids-limit",
            "128",
            "--read-only",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
        ]
        if dependency == "postgres":
            # initdb needs chown/setuid inside its private tmpfs only.
            for capability in ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"):
                options += ["--cap-add", capability]
            options += [
                "--tmpfs",
                "/var/lib/postgresql/data:rw,size=256m",
                "--tmpfs",
                "/var/run/postgresql:rw,size=8m",
            ]
        elif dependency == "qdrant":
            options += [
                "--tmpfs",
                "/qdrant/storage:rw,size=128m",
                "--tmpfs",
                "/qdrant/snapshots:rw,size=16m",
            ]
        elif dependency == "redis":
            options += ["--user", "1000:1000"]
        elif dependency == "core":
            options += [
                "--user",
                "1000:1000",
                "--tmpfs",
                "/images:rw,size=32m,uid=1000,gid=1000",
                "--tmpfs",
                "/audio:rw,size=16m,uid=1000,gid=1000",
            ]
        if fixture_volume:
            options += [
                "--mount",
                f"type=volume,source={self.volume},target=/perf-fixtures,readonly",
            ]
        if entrypoint:
            options += ["--entrypoint", entrypoint]
        for key in env or {}:
            options += ["--env", key]
        options.append(image)
        options.extend(arguments or [])
        docker(*options, environment=env)
        self.containers.append(name)
        docker("start", name)
        return name

    def inspect_container(self, name: str, dependency: str) -> dict[str, Any]:
        info = json.loads(docker("inspect", "--format=" + CONTAINER_VIEW, name))
        if isinstance(info, list):
            info = info[0]
        actual = {
            "nano_cpus": info["HostConfig"]["NanoCpus"],
            "memory_bytes": info["HostConfig"]["Memory"],
        }
        if actual != RESOURCES[dependency] or not info["HostConfig"]["ReadonlyRootfs"]:
            raise RuntimeError("Actual Docker resource/security configuration mismatch")
        if info["HostConfig"].get("PortBindings") or any(
            mount["Type"] == "bind" for mount in info["Mounts"]
        ):
            raise RuntimeError("Unexpected host port or bind mount")
        return {
            "container": name,
            "image": info["Image"],
            "ip": info["NetworkSettings"]["Networks"][self.network]["IPAddress"],
            "resources": actual,
        }

    def _fixture_volume(self) -> None:
        docker("volume", "create", "--label", LABEL + "=" + self.prefix, self.volume)
        self.volume_created = True
        docker("volume", "inspect", "--format=" + VOLUME_VIEW, self.volume)
        payload = {
            "perf_fixtures.py": (
                self.repository / "tools/ci/perf_fixtures.py"
            ).read_text(),
            "personality.yaml": (
                self.repository / "config/personality.yaml"
            ).read_text(),
        }
        writer = self.prefix + "-volume-writer"
        self.containers.append(writer)
        program = "import json,sys,pathlib,os; p=pathlib.Path('/fixture'); os.chmod(p,0o755); [(p/k).write_text(v) for k,v in json.load(sys.stdin).items()]"
        docker(
            "run",
            "--rm",
            "--name",
            writer,
            "--label",
            LABEL + "=" + self.prefix,
            "-i",
            "--pull=never",
            "--network",
            "none",
            "--cpus",
            ".1",
            "--memory",
            "67108864",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=volume,source={self.volume},target=/fixture",
            "--entrypoint",
            "/usr/local/bin/python",
            self.backend_images["fixture"],
            "-c",
            program,
            stdin=json.dumps(payload),
        )
        self.containers.remove(writer)
        verify_absent("container", writer)

    def start(self, *, certifying: bool = True) -> None:
        endpoint = docker(
            "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"
        )
        if not endpoint.startswith("unix://") or os.environ.get(
            "DOCKER_HOST", "unix://"
        ).startswith(("tcp:", "ssh:")):
            raise RuntimeError("Disposable stack requires the local Unix Docker daemon")
        for dependency, default in BACKEND_IMAGES.items():
            reference = os.environ.get(
                f"KLUKAI_PERF_{dependency.upper()}_IMAGE", default
            )
            self.backend_inputs[dependency] = reference
            self.backend_images[dependency] = inspect_image(reference, role=dependency)[
                "id"
            ]
        if certifying and self.backend_images != BACKEND_IMAGES:
            raise ValueError(
                "Certifying performance requires reviewed backend image IDs"
            )
        docker(
            "network",
            "create",
            "--internal",
            "--label",
            LABEL + "=" + self.prefix,
            self.network,
        )
        self.network_created = True
        docker("network", "inspect", "--format=" + NETWORK_VIEW, self.network)
        self._fixture_volume()
        for dependency in ("postgres", "redis", "qdrant", "fixture"):
            environment: dict[str, str] = {}
            arguments: list[str] = []
            entrypoint = None
            if dependency == "postgres":
                environment = {
                    "POSTGRES_PASSWORD": self.password,
                    "POSTGRES_USER": "perf_test",
                    "POSTGRES_DB": "postgres",
                }
            elif dependency == "redis":
                arguments = ["valkey-server", "--save", "", "--appendonly", "no"]
            elif dependency == "qdrant":
                environment = {"QDRANT__TELEMETRY_DISABLED": "true"}
            else:
                entrypoint, arguments = (
                    "/usr/local/bin/python",
                    ["/perf-fixtures/perf_fixtures.py"],
                )
            name = self._create(
                dependency,
                self.backend_images[dependency],
                env=environment,
                arguments=arguments,
                entrypoint=entrypoint,
                fixture_volume=dependency == "fixture",
            )
            self.backends[dependency] = self.inspect_container(name, dependency)
        deadline = time.monotonic() + 40
        for dependency, port in (
            ("postgres", 5432),
            ("redis", 6379),
            ("qdrant", 6333),
            ("fixture", 8088),
        ):
            while True:
                try:
                    with socket.create_connection(
                        (self.backends[dependency]["ip"], port), timeout=0.2
                    ):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"Disposable {dependency} unavailable"
                        ) from None
                    time.sleep(0.1)

    def check_temperature(self) -> None:
        self.maximum_temperature = max(self.maximum_temperature, cpu_temperature())

    def stop_core(self, name: str) -> None:
        docker("stop", "--time", "25", name)
        inspection = json.loads(docker("inspect", "--format=" + CONTAINER_VIEW, name))
        if isinstance(inspection, list):
            inspection = inspection[0]
        if inspection["State"]["OOMKilled"] or inspection["State"]["ExitCode"] != 0:
            raise RuntimeError("Candidate did not shut down gracefully")
        (self.output / f"{name}.log").write_text(docker("logs", name))
        docker("rm", "-v", name)
        verify_absent("container", name)
        self.containers.remove(name)

    def close(self) -> None:
        errors = []
        for name in reversed(self.containers):
            try:
                (self.output / f"{name}.log").write_text(docker("logs", name))
            except RuntimeError as error:
                errors.append(str(error))
            try:
                docker("rm", "-fv", name)
                verify_absent("container", name)
            except RuntimeError as error:
                errors.append(str(error))
        for kind, name, created in (
            ("volume", self.volume, self.volume_created),
            ("network", self.network, self.network_created),
        ):
            if not created:
                continue
            try:
                docker(kind, "rm", name)
                verify_absent(kind, name)
            except RuntimeError as error:
                errors.append(str(error))
        remaining = docker("ps", "-aq", "--filter", f"name={self.prefix}")
        self.cleanup_verified = not errors and not remaining
        (self.output / "cleanup.json").write_text(
            json.dumps(
                {
                    "verified": self.cleanup_verified,
                    "errors": errors,
                    "remaining_containers": remaining,
                    "prefix": self.prefix,
                },
                indent=2,
            )
            + "\n"
        )
        if not self.cleanup_verified:
            raise RuntimeError("Disposable stack cleanup failed")
