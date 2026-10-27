"""Klukai runs without the aichat stack (owner request, 2026-10-27).

Her Redis is companion-redis, her factual memory is read from her own
database (app/fact_store.py), and her external tools come only from
KLUKAI_MCP_CONFIG. These checks fail if core, the events bridge, the compose
file or the tailnet gateway points at an aichat container or network again.
The database keeps its old name ("aichat" on infra-postgres); that is
Klukai's own data, not the aichat stack, so DATABASE_URL is exempt.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[1]
REPO = CORE.parents[1]

# Container names, compose aliases and the stack's network.
AICHAT = re.compile(
    r"aichat-(aichat-)?(data|mcp|redis|vector|inference|minio|auth|web|searxng|"
    r"sandbox|docs|vision|browser|whatsapp|jupyter)\b|aichat_default|\bMCP_URL\b|\bDATA_URL\b"
)


def _code_strings(path: Path) -> list[tuple[int, str]]:
    """String literals and identifiers in Python code, without docstrings or
    comments: history may name the old services, code may not."""
    tree = ast.parse(path.read_text())
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                found.append((node.lineno, node.value))
        elif isinstance(node, ast.Name):
            found.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute):
            found.append((node.lineno, node.attr))
    return found


def _python_hits(paths: list[Path]) -> list[str]:
    return [
        f"{path.relative_to(REPO)}:{line}"
        for path in paths
        for line, text in _code_strings(path)
        if AICHAT.search(text)
    ]


def _config_hits(path: Path) -> list[str]:
    return [
        f"{path.relative_to(REPO)}:{number}"
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if AICHAT.search(line.split("#", 1)[0])
    ]


def _python(directory: Path) -> list[Path]:
    return sorted(
        p
        for p in directory.rglob("*.py")
        if "__pycache__" not in p.parts and "tests" not in p.parts
    )


def test_core_code_names_no_aichat_service() -> None:
    assert not _python_hits(_python(CORE / "app"))


def test_events_bridge_names_no_aichat_service() -> None:
    assert not _python_hits(_python(REPO / "docker/events-bridge"))


@pytest.mark.parametrize(
    "name", ["docker-compose.yml", "gateway/docker-compose.yml", "gateway/nginx.conf"]
)
def test_deployment_files_name_no_aichat_service(name: str) -> None:
    assert not _config_hits(REPO / name)


def test_redis_is_klukais_own_and_password_protected() -> None:
    compose = (REPO / "docker-compose.yml").read_text()
    urls = re.findall(r'REDIS_URL: "([^"]+)"', compose)
    assert len(urls) == 2  # core and the events bridge
    assert all(
        u.startswith("redis://:${KLUKAI_REDIS_PASSWORD:?") and "@companion-redis:" in u
        for u in urls
    )
    redis = compose.split("\n  redis:\n", 1)[1].split("\n\n", 1)[0]
    assert "container_name: companion-redis" in redis
    assert "ports:" not in redis
    assert "requirepass $$KLUKAI_REDIS_PASSWORD" in redis
