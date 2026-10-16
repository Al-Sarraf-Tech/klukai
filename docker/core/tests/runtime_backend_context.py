"""Validate host-owned backend context before opening any real connection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

PORTS = {"postgres": 5432, "redis": 6379, "qdrant": 6333}
MODULES = {
    "tests/runtime_backends.py",
    "tests/health_runtime_backends.py",
    "tests/lifecycle_runtime_backends.py",
}


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def regular(path: Path) -> None:
    require(
        path.is_absolute() and path.is_file(),
        "Context input is not a regular absolute file",
    )
    require(
        not any(p.is_symlink() for p in (path, *path.parents)), "Linked context input"
    )


def validate_bindings(bindings: Any, source: Path, module: str) -> None:
    required = {
        "docker/core/" + module,
        "docker/core/tests/runtime_backend_support.py",
        "docker/core/tests/runtime_backend_context.py",
        "docker/core/requirements-ci.txt",
    }
    require(
        isinstance(bindings, dict) and required <= bindings.keys(),
        "Missing source bindings",
    )
    for name, digest in bindings.items():
        require(
            isinstance(name, str) and isinstance(digest, str),
            "Malformed source binding",
        )
        pure = PurePosixPath(name)
        require(
            not pure.is_absolute()
            and ".." not in pure.parts
            and ".git" not in pure.parts,
            "Unsafe source binding",
        )
        require(re.fullmatch(r"[a-f0-9]{64}", digest), "Malformed source hash")
        path = source / name
        regular(path)
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        require(actual == digest, "Managed source differs from committed snapshot")


def load_context(path: Path, digest: str, module: str, source: Path) -> dict[str, Any]:
    regular(path)
    require(not path.stat().st_mode & 0o222, "Context must be readonly")
    require(0 < path.stat().st_size <= 8 * 1024**2, "Invalid context size")
    require(re.fullmatch(r"[a-f0-9]{64}", digest), "Missing exact context hash")
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == digest, "Context hash differs")
    data: Any = json.loads(raw)
    require(
        isinstance(data, dict)
        and set(data)
        == {
            "schema_version",
            "token",
            "revision",
            "module",
            "source_sha256",
            "password",
            "backends",
        },
        "Unknown managed context schema",
    )
    require(
        type(data["schema_version"]) is int and data["schema_version"] == 1,
        "Invalid context version",
    )
    require(
        isinstance(data["token"], str) and re.fullmatch(r"[a-f0-9]{32}", data["token"]),
        "Invalid ownership token",
    )
    require(
        isinstance(data["revision"], str)
        and re.fullmatch(r"[a-f0-9]{40}", data["revision"]),
        "Invalid source revision",
    )
    require(module in MODULES and data["module"] == module, "Context driver mismatch")
    password = data["password"]
    require(
        isinstance(password, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", password),
        "Missing generated credential",
    )
    endpoints = data["backends"]
    require(
        isinstance(endpoints, dict) and set(endpoints) == set(PORTS),
        "Missing disposable backend",
    )
    for name, port in PORTS.items():
        value = endpoints[name]
        require(
            isinstance(value, dict) and set(value) == {"host", "port", "image_id"},
            "Malformed backend endpoint",
        )
        require(
            value["host"] == f"backend-{data['token']}-{name}",
            "Backend alias is not owned",
        )
        require(
            type(value["port"]) is int and value["port"] == port, "Invalid backend port"
        )
        require(
            isinstance(value["image_id"], str)
            and re.fullmatch(r"sha256:[a-f0-9]{64}", value["image_id"]),
            "Mutable backend image",
        )
    validate_bindings(data["source_sha256"], source, module)
    return dict(data)
