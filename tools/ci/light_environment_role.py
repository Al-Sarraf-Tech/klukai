"""Fixed committed job closures; consumers cannot supply a lock."""

from __future__ import annotations

from pathlib import Path

from . import python_executor, tool_materials
from .core_source import require

TOOLS_LOCK = "docker/core/requirements-ci.txt"
BRIDGE_LOCK = "docker/events-bridge/requirements-ci.txt"
PERF_LOCK = "docker/core/requirements-perf.txt"
PERF_PACKAGES = {
    "anyio",
    "certifi",
    "h11",
    "httpcore",
    "httpx",
    "idna",
    "pip",
    "psycopg",
    "psycopg-binary",
    "redis",
    "typing-extensions",
}


def lock(role: str) -> str:
    require(role in {"tools", "bridge", "perf"}, "Unknown native environment role")
    return {"tools": TOOLS_LOCK, "bridge": BRIDGE_LOCK, "perf": PERF_LOCK}[role]


def inputs(role: str) -> tuple[str, ...]:
    lock(role)
    if role == "perf":
        return (PERF_LOCK, "docker/core/requirements-perf.in")
    return (
        ()
        if role == "tools"
        else (
            BRIDGE_LOCK,
            "docker/events-bridge/requirements-ci.in",
            "docker/events-bridge/requirements.txt",
        )
    )


def expected(root: Path, role: str) -> dict[str, str]:
    text = (root / lock(role)).read_text()
    result = python_executor.lock_versions(text)
    tool_materials.validate_hash_lock(text, len(result) if role == "tools" else 11)
    if role == "bridge":
        require(
            result.get("pika") == "1.3.2" and result.get("redis") == "5.2.1",
            "Bridge runtime pins differ from its isolated boundary",
        )
    elif role == "perf":
        complete = python_executor.lock_versions((root / TOOLS_LOCK).read_text())
        require(
            set(result) == PERF_PACKAGES
            and all(result[name] == complete.get(name) for name in result),
            "Perf closure or pins differ from the complete CI lock",
        )
    return result
