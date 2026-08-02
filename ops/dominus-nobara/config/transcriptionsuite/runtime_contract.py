#!/usr/bin/env python3
"""Seal and verify the exact TranscriptionSuite v1.3.7 RAID runtime."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


IMAGE_MANIFEST = "sha256:9b9587d6db3dbc6e06ab5df3498798a9f194bf853156fc2fe62b1b1771dba1b0"
SOURCE_COMMIT = "f1760a17d163fab2a1bce4e40fa581336aa95dcd"
UV_LOCK_SHA256 = "9dc0f358e5a26bd052d7e2fee09e42224a1076789d9ce89ae9a1e31e0281f811"
PYPROJECT_SHA256 = "522381de1e9cd91952d5513aaa917bfc9ae8604e693c3250264fd2ef1a2c04ff"
BOOTSTRAP_SHA256 = "b73a0042414a19c3a9c8990104c3f5475003b410d6fe9826282f55f62329a93b"

RUNTIME_ROOT = Path("/runtime")
CONTRACT_ROOT = RUNTIME_ROOT / "dominus-bootstrap"
MARKER_PATH = CONTRACT_ROOT / "runtime-contract.json"
FREEZE_PATH = CONTRACT_ROOT / "packages.freeze"
STATUS_PATH = RUNTIME_ROOT / "bootstrap-status.json"
UV_LOCK_PATH = Path("/app/server/uv.lock")
PYPROJECT_PATH = Path("/app/server/pyproject.toml")
BOOTSTRAP_PATH = Path("/app/docker/bootstrap_runtime.py")
VENV_PYTHON = RUNTIME_ROOT / ".venv/bin/python"
UV = Path("/usr/local/bin/uv")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def locked_source_hashes() -> dict[str, str]:
    return {
        "uv_lock_sha256": sha256_file(UV_LOCK_PATH),
        "pyproject_sha256": sha256_file(PYPROJECT_PATH),
        "bootstrap_sha256": sha256_file(BOOTSTRAP_PATH),
    }


def require_locked_sources() -> None:
    actual = locked_source_hashes()
    expected = {
        "uv_lock_sha256": UV_LOCK_SHA256,
        "pyproject_sha256": PYPROJECT_SHA256,
        "bootstrap_sha256": BOOTSTRAP_SHA256,
    }
    if actual != expected:
        raise RuntimeError(f"pinned v1.3.7 source hash mismatch: {actual}")


def package_freeze() -> bytes:
    if not VENV_PYTHON.is_file() or not os.access(VENV_PYTHON, os.X_OK):
        raise RuntimeError(f"runtime Python is missing: {VENV_PYTHON}")
    result = subprocess.run(
        [str(UV), "pip", "freeze", "--python", str(VENV_PYTHON)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    lines = sorted(line.strip() for line in result.stdout.splitlines() if line.strip())
    return b"\n".join(lines) + b"\n"


def require_features() -> str:
    document = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    features = document.get("features", {})
    missing = [
        name
        for name in ("whisper", "nemo")
        if not isinstance(features.get(name), dict)
        or not features[name].get("available", False)
    ]
    if missing:
        raise RuntimeError(f"required bootstrap features unavailable: {', '.join(missing)}")
    return sha256_file(STATUS_PATH)


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
    with temporary.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def seal() -> None:
    require_locked_sources()
    status_sha256 = require_features()
    frozen = package_freeze()
    CONTRACT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(FREEZE_PATH, frozen, 0o600)
    marker = {
        "version": 1,
        "image_manifest": IMAGE_MANIFEST,
        "source_commit": SOURCE_COMMIT,
        "uv_lock_sha256": UV_LOCK_SHA256,
        "pyproject_sha256": PYPROJECT_SHA256,
        "bootstrap_sha256": BOOTSTRAP_SHA256,
        "bootstrap_status_sha256": status_sha256,
        "packages_freeze_sha256": hashlib.sha256(frozen).hexdigest(),
        "install_nemo": True,
        "install_whisper": True,
        "network_required_after_seal": False,
    }
    atomic_write(
        MARKER_PATH,
        (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        0o600,
    )
    print(json.dumps({"status": "sealed", **marker}, sort_keys=True))


def verify() -> None:
    require_locked_sources()
    marker = json.loads(MARKER_PATH.read_text(encoding="utf-8"))
    expected = {
        "version": 1,
        "image_manifest": IMAGE_MANIFEST,
        "source_commit": SOURCE_COMMIT,
        "uv_lock_sha256": UV_LOCK_SHA256,
        "pyproject_sha256": PYPROJECT_SHA256,
        "bootstrap_sha256": BOOTSTRAP_SHA256,
        "install_nemo": True,
        "install_whisper": True,
        "network_required_after_seal": False,
    }
    for key, value in expected.items():
        if marker.get(key) != value:
            raise RuntimeError(f"runtime marker mismatch for {key}")
    # The upstream entrypoint rewrites generated_at on each offline sync, so
    # validate required feature semantics rather than treating that timestamped
    # status document as immutable.
    require_features()
    frozen = package_freeze()
    if marker.get("packages_freeze_sha256") != hashlib.sha256(frozen).hexdigest():
        raise RuntimeError("installed TranscriptionSuite package set drifted")
    if FREEZE_PATH.read_bytes() != frozen:
        raise RuntimeError("package freeze ledger does not match the runtime")
    print(json.dumps({"status": "verified", "network_required": False}, sort_keys=True))


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"seal", "verify"}:
        raise SystemExit("usage: runtime_contract.py {seal|verify}")
    if sys.argv[1] == "seal":
        seal()
    else:
        verify()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"transcriptionsuite-runtime-contract: {error}", file=sys.stderr)
        raise SystemExit(1) from error
