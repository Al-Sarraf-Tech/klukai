"""Verify immutable release bytes before allowing pickle checkpoint loading."""
from __future__ import annotations

import hashlib
import importlib.metadata
from pathlib import Path


def verify_file(path: Path, record: dict) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"artifact is not a regular file: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    if path.stat().st_size != record["size_bytes"] or digest.hexdigest() != record["sha256"]:
        raise ValueError(f"artifact digest mismatch: {path.name}")


def verify_artifacts(lock: dict, model_dir: Path, reference: Path) -> None:
    for record in lock["model"]["files"]:
        name = Path(record["path"])
        if name.is_absolute() or ".." in name.parts:
            raise ValueError("invalid model artifact path")
        verify_file(model_dir / name, record)
    verify_file(reference, lock["reference"])


def verify_packages(packages: dict[str, str]) -> None:
    for name, expected in packages.items():
        actual = importlib.metadata.version(name)
        if actual != expected:
            raise ValueError(f"runtime drift: {name} expected {expected}, found {actual}")
