#!/usr/bin/env python3
"""Build standard, writable Hugging Face cache views from a locked release.

The immutable model release remains mounted read-only. Files are verified
against models.lock.json, then cloned into separate Speaches and
TranscriptionSuite cache trees with XFS reflinks when supported. There is no
network code and the Compose maintenance service has no network interface.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any


LOCK_PATH = Path(os.environ.get("MODEL_LOCK_PATH", "/config/models.lock.json"))
RELEASE_ROOT = Path(os.environ.get("MODEL_RELEASE_ROOT", "/release"))
CACHE_ROOTS = {
    "speaches": Path(os.environ.get("SPEACHES_HF_HOME", "/caches/speaches")),
    "transcription": Path(
        os.environ.get("TRANSCRIPTION_HF_HOME", "/caches/transcription")
    ),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified(path: Path, size: int, sha256: str) -> bool:
    return path.is_file() and path.stat().st_size == size and sha256_file(path) == sha256


def snapshot_targets(destination: str) -> tuple[str, ...]:
    if destination.startswith("speech/shared/"):
        return ("speaches", "transcription")
    if destination.startswith("speaches/"):
        return ("speaches",)
    if destination.startswith("transcription/"):
        return ("transcription",)
    return ()


def repo_cache_dir(cache_root: Path, repo_id: str) -> Path:
    owner, separator, name = repo_id.partition("/")
    if not separator or not owner or not name:
        raise ValueError(f"invalid Hugging Face repo id: {repo_id!r}")
    return cache_root / "hub" / f"models--{owner}--{name.replace('/', '--')}"


def clone_verified(source: Path, destination: Path, size: int, sha256: str) -> None:
    if not verified(source, size, sha256):
        raise RuntimeError(f"release file failed size/SHA-256 verification: {source}")
    if verified(destination, size, sha256):
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        temporary.unlink()
        reflink = subprocess.run(
            [
                "cp",
                "--reflink=always",
                "--preserve=mode,timestamps",
                "--",
                str(source),
                str(temporary),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if reflink.returncode != 0:
            subprocess.run(
                [
                    "cp",
                    "--reflink=auto",
                    "--preserve=mode,timestamps",
                    "--",
                    str(source),
                    str(temporary),
                ],
                check=True,
            )
        if not verified(temporary, size, sha256):
            raise RuntimeError(f"materialized file failed verification: {temporary}")
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
    temporary.write_text(value, encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def materialize_snapshot(snapshot: dict[str, Any], target: str) -> int:
    destination = str(snapshot["destination"])
    source_info = snapshot["source"]
    repo_id = str(source_info["repo"])
    revision = str(source_info["revision"])
    source_root = RELEASE_ROOT / destination
    repo_root = repo_cache_dir(CACHE_ROOTS[target], repo_id)
    snapshot_root = repo_root / "snapshots" / revision
    blob_root = repo_root / "blobs"

    files = snapshot.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError(f"snapshot {snapshot.get('id')} has no locked files")
    if target == "speaches" and not any(
        isinstance(item, dict) and item.get("path") == "README.md" for item in files
    ):
        raise RuntimeError(
            f"snapshot {snapshot.get('id')} lacks README.md required by Speaches model discovery"
        )

    copied = 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError(f"snapshot {snapshot.get('id')} has a non-object file entry")
        relative = Path(str(item["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe snapshot path: {relative}")
        size = int(item["size_bytes"])
        sha256 = str(item["sha256"] or "")
        if len(sha256) != 64:
            raise ValueError(f"snapshot file is not content locked: {snapshot.get('id')}/{relative}")

        source = source_root / relative
        blob = blob_root / sha256
        clone_verified(source, blob, size, sha256)

        link = snapshot_root / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        expected_target = os.path.relpath(blob, link.parent)
        if link.is_symlink() and os.readlink(link) == expected_target:
            pass
        else:
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(expected_target)
        copied += 1

    atomic_text(repo_root / "refs" / "main", f"{revision}\n")
    return copied


def main() -> int:
    document = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    snapshots = document.get("snapshots")
    if not isinstance(snapshots, list):
        raise ValueError("models.lock.json snapshots must be an array")

    tasks: list[tuple[dict[str, Any], str]] = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or not snapshot.get("enabled", False):
            continue
        for target in snapshot_targets(str(snapshot.get("destination", ""))):
            tasks.append((snapshot, target))
    if not tasks:
        raise RuntimeError("no enabled Speaches/TranscriptionSuite snapshots found")

    workers = min(20, max(1, int(os.environ.get("MATERIALIZE_WORKERS", "8"))))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(lambda pair: materialize_snapshot(*pair), tasks))

    summary = {
        "status": "ok",
        "snapshots_materialized": len(tasks),
        "files_verified": sum(results),
        "network_used": False,
        "cache_roots": {key: str(value) for key, value in CACHE_ROOTS.items()},
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"materialize-hf-cache: {error}", file=sys.stderr)
        raise SystemExit(1) from error
