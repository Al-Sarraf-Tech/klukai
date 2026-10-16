"""Maintained application code measured by size and Python complexity gates.

The separately attributed Flutter SDK output, CanvasKit, Three.js/VRM and
Pretext distributions are vendor/generated artifacts, not handwritten runtime
modules. Their locked provenance and executable behavior have separate gates.
"""

from pathlib import Path

PYTHON_ROOTS = (
    "docker/core/app",
    "docker/voice/app",
    "docker/events-bridge/main.py",
    "tgbot",
    "ops/dominus-nobara/lmstudio-compat/gateway",
)
OTHER_ROOTS = (("flutter_app/lib", ".dart"), ("flutter_app/web/companion", ".js"))


def python_sources(root: Path) -> list[Path]:
    files = set()
    for name in PYTHON_ROOTS:
        path = root / name
        candidates = [path] if path.is_file() else path.rglob("*.py")
        for candidate in candidates:
            relative = candidate.relative_to(root)
            if not ({"test", "tests", "__pycache__"} & set(relative.parts)):
                files.add(candidate)
    return sorted(files)


def runtime_sources(root: Path) -> list[Path]:
    return python_sources(root) + [
        path
        for name, suffix in OTHER_ROOTS
        for path in sorted((root / name).rglob("*" + suffix))
    ]
