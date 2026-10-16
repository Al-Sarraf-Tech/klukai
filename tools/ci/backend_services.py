"""Host-owned pinned backend services; no Docker authority enters the worker."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
import re
import time
from typing import Any
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .backend_profile import validate_closed_host
from .core_profile import (
    environment_flags,
    postgres_flags,
    validate_capabilities,
    validate_environment,
    validate_postgres_profile,
    validate_private_attachment,
)
from .core_resources import Docker, ResourceFailure, Resources

PORTS = {"postgres": 5432, "redis": 6379, "qdrant": 6333}
LIMITS = {
    "postgres": (500_000_000, 384),
    "redis": (300_000_000, 96),
    "qdrant": (800_000_000, 384),
}
TEMPORARY = {
    "redis": {"/tmp": 32, "/data": 64},
    "qdrant": {"/tmp": 32, "/qdrant/storage": 128},
}


def alias(token: str, name: str) -> str:
    return f"backend-{token}-{name}"


def service_flags(name: str, network: str, token: str) -> list[str]:
    hostname = alias(token, name)
    if name == "postgres":
        return [
            f"--network-alias={hostname}"
            if flag == "--network-alias=postgres"
            else flag
            for flag in postgres_flags(network)
        ]
    cpu, memory = LIMITS[name]
    return [
        "--user=1000:1000",
        "--read-only",
        f"--cpus={cpu / 1e9:g}",
        f"--memory={memory}m",
        f"--memory-swap={memory}m",
        "--pids-limit=128",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--ipc=private",
        "--network=" + network,
        "--network-alias=" + hostname,
        "--dns=127.0.0.1",
        *[
            f"--tmpfs={path}:rw,noexec,nosuid,nodev,size={size}m,mode=1777"
            for path, size in TEMPORARY[name].items()
        ],
    ]


def service_environment(name: str, password: str) -> dict[str, str]:
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
    if name == "postgres":
        env.update(
            {
                "POSTGRES_USER": "runtime_test",
                "POSTGRES_DB": "runtime_test",
                "POSTGRES_PASSWORD": password,
                "PGDATA": "/var/lib/postgresql/data/pgdata",
                "PG_MAJOR": "17",
                "LANG": "en_US.utf8",
            }
        )
    elif name == "qdrant":
        env.update(
            {
                "QDRANT__TELEMETRY_DISABLED": "true",
                "QDRANT__STORAGE__SNAPSHOTS_PATH": "/qdrant/storage/snapshots",
            }
        )
    return env


def validate_service(
    info: dict[str, Any] | None,
    name: str,
    network: str,
    env: dict[str, str],
    token: str,
) -> None:
    if info is None:
        raise ResourceFailure("Backend container disappeared")
    validate_closed_host(info)
    if name == "postgres":
        validate_postgres_profile(info, network, env)
    else:
        cpu, memory = LIMITS[name]
        expected = {
            "ReadonlyRootfs": True,
            "NanoCpus": cpu,
            "Memory": memory * 1024**2,
            "MemorySwap": memory * 1024**2,
            "PidsLimit": 128,
            "Privileged": False,
            "NetworkMode": network,
            "Dns": ["127.0.0.1"],
            "IpcMode": "private",
            "PidMode": "",
        }
        if any(info["HostConfig"].get(key) != value for key, value in expected.items()):
            raise ResourceFailure("Actual backend isolation/resources differ")
        validate_capabilities(info, "1000:1000")
        validate_private_attachment(info, network)
        validate_environment(info, env)
        if info.get("Mounts"):
            raise ResourceFailure("Backend must not have host/volume mounts")
        temporary = info["HostConfig"].get("Tmpfs") or {}
        if set(temporary) != set(TEMPORARY[name]) or any(
            not {"noexec", "nosuid", "nodev"} <= set(value.split(","))
            for value in temporary.values()
        ):
            raise ResourceFailure("Unsafe backend temporary filesystems")
    endpoint = info["NetworkSettings"]["Networks"][network]
    aliases = endpoint.get("Aliases") or []
    if alias(token, name) not in aliases:
        raise ResourceFailure("Missing owned backend network alias")
    allowed = {
        alias(token, name),
        info.get("Id", "")[:12],
        info.get("Name", "").lstrip("/"),
    }
    if set(aliases) - allowed:
        raise ResourceFailure("Unexpected backend network alias")


def postgres_version(docker: Docker, cid: str, password: str) -> str:
    version = docker.run(
        "exec",
        "--env=PGPASSWORD",
        cid,
        "psql",
        "-h",
        "127.0.0.1",
        "-U",
        "runtime_test",
        "-d",
        "runtime_test",
        "-Atc",
        "SELECT current_setting('server_version_num')",
        environment={"PGPASSWORD": password},
    ).stdout.strip()
    if not version.isdigit() or not 170000 <= int(version) < 180000:
        raise ResourceFailure("Actual PostgreSQL major version is not17")
    return version


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise ResourceFailure("Disposable backend attempted an HTTP redirect")


def qdrant_version(
    info: dict[str, Any], network: str, evidence: Path | None = None
) -> str:
    networks = info.get("NetworkSettings", {}).get("Networks", {})
    if set(networks) != {network}:
        raise ResourceFailure("Qdrant is not on its owned private network")
    try:
        address = ipaddress.IPv4Address(networks[network]["IPAddress"])
    except (ValueError, KeyError) as error:
        raise ResourceFailure("Missing owned Qdrant address") from error
    if (
        not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
    ):
        raise ResourceFailure("Qdrant address is not a private container endpoint")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(f"http://{address}:6333/", timeout=0.5) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ResourceFailure("Oversized disposable Qdrant response")
    if evidence is not None:
        evidence.write_bytes(raw)
    data = json.loads(raw)
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+", version
    ):
        raise ResourceFailure("Missing actual Qdrant server version")
    return version


def ready_service(
    docker: Docker, cid: str, name: str, password: str, network: str
) -> str:
    commands = {
        "postgres": [
            "pg_isready",
            "-h",
            "127.0.0.1",
            "-U",
            "runtime_test",
            "-d",
            "runtime_test",
        ],
        "redis": ["valkey-cli", "--raw", "PING"],
    }
    deadline = time.monotonic() + 60
    version = ""
    while True:
        if name == "qdrant":
            info = docker.container(cid)
            if info is None:
                raise ResourceFailure("Owned Qdrant disappeared before readiness")
            try:
                version = qdrant_version(
                    info, network, docker.output / "qdrant-server.json"
                )
                break
            except (URLError, TimeoutError, ConnectionError):
                pass
        elif docker.run("exec", cid, *commands[name], check=False).returncode == 0:
            break
        if time.monotonic() >= deadline:
            raise ResourceFailure("Disposable backend readiness/version timed out")
        time.sleep(0.3)
    uid = docker.run("exec", cid, "id", "-u").stdout.strip()
    if not uid.isdigit() or int(uid) == 0:
        raise ResourceFailure("Actual backend process execution is not nonroot")
    if name == "postgres":
        return postgres_version(docker, cid, password)
    if name == "qdrant":
        return version
    raw = docker.run("exec", cid, "valkey-cli", "--raw", "INFO", "server").stdout
    match = re.search(
        r"^valkey_version:([0-9]+\.[0-9]+\.[0-9]+)\r?$", raw, re.MULTILINE
    )
    if match is None:
        raise ResourceFailure("Missing actual Valkey server version")
    return match[1]


def start_services(
    resources: Resources, images: dict[str, dict[str, Any]], password: str
) -> dict[str, Any]:
    report = {}
    for name in PORTS:
        env = service_environment(name, password)
        cid = resources.create_container(
            name,
            images[name]["Id"],
            service_flags(name, resources.network, resources.token)
            + environment_flags(images[name], env),
            environment=env,
            command=["valkey-server", "--save", "", "--appendonly", "no"]
            if name == "redis"
            else None,
        )
        resources.docker.run("start", cid)
        info = resources.docker.container(cid)
        validate_service(info, name, resources.network, env, resources.token)
        if (
            info is None
            or info.get("Id") != cid
            or info.get("Image") != images[name]["Id"]
            or info["State"].get("Running") is not True
        ):
            raise ResourceFailure("Actual started backend identity/state differs")
        version = ready_service(
            resources.docker, cid, name, password, resources.network
        )
        report[name] = {
            "id": cid,
            "image_id": images[name]["Id"],
            "version": version,
            "host": alias(resources.token, name),
            "port": PORTS[name],
        }
    return report
