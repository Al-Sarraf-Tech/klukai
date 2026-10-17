"""Retain the exact CI-built core image and verify it before deployment.

CI (`save`) writes `core-image.tar` plus `manifest.json` into the run's
private artifact directory on the heavy guest. The deployment host (`verify`)
checks the transferred tar against the manifest, loads it, and confirms the
loaded image carries the same filesystem layers and revision label. Image IDs
are not compared: the containerd and classic Docker stores report different
IDs for the same image, while layer diff IDs are content addresses in both.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

LABEL = "org.opencontainers.image.revision"
SCHEMA = 1


def _docker(*args: str, timeout: int = 600) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def _revision(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("A full 40-character revision is required")
    return value


def identity(image: str) -> dict[str, Any]:
    info = json.loads(_docker("image", "inspect", image))[0]
    labels = (info.get("Config") or {}).get("Labels") or {}
    layers = (info.get("RootFS") or {}).get("Layers") or []
    if not layers or not all(re.fullmatch(r"sha256:[0-9a-f]{64}", x) for x in layers):
        raise ValueError("Image has no content-addressed layers")
    return {
        "revision": labels.get(LABEL),
        "layers": layers,
        "user": info["Config"].get("User"),
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save(image: str, revision: str, output: Path, run: str) -> dict[str, Any]:
    revision = _revision(revision)
    found = identity(image)
    if found["revision"] != revision:
        raise ValueError("Image revision label differs from the CI revision")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    tar = output / "core-image.tar"
    _docker("save", "--output", str(tar), image, timeout=900)
    manifest = {
        "schema": SCHEMA,
        "revision": revision,
        "run": run,
        "image_ref": image,
        "layers": found["layers"],
        "user": found["user"],
        "tar_sha256": sha256(tar),
        "tar_bytes": tar.stat().st_size,
        "saved_at": datetime.now(UTC).isoformat(),
    }
    fd = os.open(output / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
    return manifest


def check_manifest(manifest: dict[str, Any], revision: str) -> None:
    if (
        manifest.get("schema") != SCHEMA
        or manifest.get("revision") != _revision(revision)
        or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("tar_sha256", "")))
        or not isinstance(manifest.get("layers"), list)
        or not manifest["layers"]
    ):
        raise ValueError("Release manifest is malformed or for another revision")


def verify(tar: Path, manifest_path: Path, revision: str, tag: str) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text())
    check_manifest(manifest, revision)
    if sha256(tar) != manifest["tar_sha256"]:
        raise ValueError("Transferred image tar differs from the CI manifest")
    loaded = _docker("load", "--input", str(tar), timeout=900)
    match = re.search(r"Loaded image(?: ID)?: (\S+)", loaded)
    if not match:
        raise RuntimeError("docker load did not report the loaded image")
    found = identity(match.group(1))
    if found["layers"] != manifest["layers"] or found["revision"] != revision:
        raise ValueError("Loaded image layers or revision differ from the CI manifest")
    _docker("tag", match.group(1), tag)
    return {"tag": tag, "revision": revision, "layers": len(found["layers"])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("save")
    s.add_argument("--image", required=True)
    s.add_argument("--revision", required=True)
    s.add_argument("--output", type=Path, required=True)
    s.add_argument("--run", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--tar", type=Path, required=True)
    v.add_argument("--manifest", type=Path, required=True)
    v.add_argument("--revision", required=True)
    v.add_argument("--tag", required=True)
    args = parser.parse_args()
    try:
        if args.command == "save":
            result = save(args.image, args.revision, args.output, args.run)
        else:
            result = verify(args.tar, args.manifest, args.revision, args.tag)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"release artifact refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
