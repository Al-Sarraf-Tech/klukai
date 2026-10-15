"""Executable boundaries: domain/storage primitives cannot depend on the UI.

These are dependency rules, not a claim that module size alone is architecture.
The compatibility facades may import their cohesive implementations; low-level
modules cannot import the HTTP/chat orchestration back through those facades.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "docker/core"
HTTP = ("fastapi", "starlette", "app.main", "app.routes", "app.chat")


def imports(source: str, module: str, *, package: bool = False) -> set[str]:
    found: set[str] = set()
    parent = module.split(".") if package else module.split(".")[:-1]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = parent[: len(parent) - node.level + 1] if node.level else []
            if node.module:
                base += node.module.split(".")
            target = ".".join(base)
            found.add(target)
            found.update(f"{target}.{alias.name}" for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            function = ast.unparse(node.func)
            if function in {"__import__", "importlib.import_module"} and isinstance(
                node.args[0].value, str
            ):
                found.add(node.args[0].value)
    return found


def forbidden(targets: set[str], prefixes: tuple[str, ...]) -> set[str]:
    return {
        name
        for name in targets
        if any(
            name == p or name.startswith(p + ".") or name.startswith(p + "_")
            for p in prefixes
        )
    }


@pytest.mark.parametrize(
    "source",
    [
        "import app.main",
        "from app import routes",
        "from ..chat_handlers import chat",
        "importlib.import_module('app.main')",
    ],
)
def test_boundary_checker_rejects_reverse_dependencies(source):
    assert forbidden(imports(source, "app.personality.speech"), HTTP)


def test_boundary_checker_resolves_package_relative_imports():
    assert forbidden(
        imports("from .. import main", "app.personality", package=True), HTTP
    )


def test_personality_remains_independent_of_transport_and_persistence():
    failures = []
    for path in sorted((CORE / "app/personality").glob("*.py")):
        parts = path.relative_to(CORE).with_suffix("").parts
        module = ".".join(parts[:-1] if path.name == "__init__.py" else parts)
        blocked = forbidden(
            imports(path.read_text(), module, package=path.name == "__init__.py"),
            HTTP
            + ("app.auth", "app.db", "app.memory", "app.image_gen", "app.voice_jobs"),
        )
        if blocked:
            failures.append(f"{path.name}: {sorted(blocked)}")
    assert not failures, "Domain depends on delivery/storage: " + "; ".join(failures)


@pytest.mark.parametrize(
    "module",
    [
        "db",
        "passwords",
        "audit_chain",
        "metrics",
        "circuit_breakers",
        "observability.correlation",
        "observability.redaction",
        "observability.trace_redaction",
    ],
)
def test_primitives_do_not_depend_on_http_or_chat_orchestration(module):
    path = CORE / "app" / Path(*module.split(".")).with_suffix(".py")
    assert path.is_file()
    assert not forbidden(imports(path.read_text(), f"app.{module}"), HTTP)


def test_flutter_models_do_not_import_delivery_or_network_services():
    failures = []
    for path in sorted((ROOT / "flutter_app/lib/models").rglob("*.dart")):
        for target in re.findall(
            r"^\s*import\s+['\"]([^'\"]+)", path.read_text(), re.MULTILINE
        ):
            if any(
                segment in target.split("/")
                for segment in ("screens", "widgets", "services", "platform")
            ):
                failures.append(f"{path.name}: {target}")
    assert not failures, "Models depend on delivery/network: " + "; ".join(failures)
