"""Fixed committed tools and bridge closures; consumers cannot supply a lock."""

from __future__ import annotations

from pathlib import Path

from . import python_executor, tool_materials
from .core_source import require

TOOLS_LOCK = "docker/core/requirements-ci.txt"
BRIDGE_LOCK = "docker/events-bridge/requirements-ci.txt"


def lock(role: str) -> str:
    require(role in {"tools", "bridge"}, "Unknown native environment role")
    return TOOLS_LOCK if role == "tools" else BRIDGE_LOCK


def inputs(role: str) -> tuple[str, ...]:
    lock(role)
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
    return result
