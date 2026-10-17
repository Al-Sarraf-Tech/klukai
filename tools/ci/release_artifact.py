"""Retain the exact CI-built core image and verify it before deployment.

CI (`save`) writes `core-image.tar`, the provenance materials (CycloneDX SBOM,
licence inventory and policy verdict, second-build reproducibility result) and
`manifest.json` into the run's private artifact directory on the heavy guest.
The manifest binds them to the revision, source tree, base images and tool
versions. The deployment host (`verify`) checks the transferred tar against the
manifest, loads it, and confirms the loaded image carries the same filesystem
layers and revision label. Image IDs are not compared: the containerd and
classic Docker stores report different IDs for the same image, while layer diff
IDs are content addresses in both.
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
SCHEMA = 2
MATERIALS = (
    "sbom.cdx.json",
    "licenses.json",
    "license-verdict.json",
    "reproducibility.json",
)


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


def base_images(dockerfile: Path) -> dict[str, str]:
    """Digest-pinned base images declared as ARG defaults in the Dockerfile."""
    found = dict(
        re.findall(
            r"^ARG (\w+_IMAGE)=(\S+@sha256:[0-9a-f]{64})$", dockerfile.read_text(), re.M
        )
    )
    if not found:
        raise ValueError("Dockerfile declares no digest-pinned base images")
    return found


def save(
    image: str,
    revision: str,
    output: Path,
    run: str,
    *,
    materials: dict[str, Path],
    tools: dict[str, str],
    dockerfile: Path,
    source_tree: str,
) -> dict[str, Any]:
    revision = _revision(revision)
    found = identity(image)
    if found["revision"] != revision:
        raise ValueError("Image revision label differs from the CI revision")
    if set(materials) != set(MATERIALS) or not tools:
        raise ValueError("Provenance materials or tool identities are incomplete")
    if not re.fullmatch(r"[0-9a-f]{40}", source_tree):
        raise ValueError("A full source tree object id is required")
    verdict = json.loads(materials["license-verdict.json"].read_text())
    if verdict.get("passed") is not True:
        raise ValueError("Licence policy did not pass")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    recorded = {}
    for name in MATERIALS:
        target = output / name
        target.write_bytes(materials[name].read_bytes())
        recorded[name] = sha256(target)
    tar = output / "core-image.tar"
    _docker("save", "--output", str(tar), image, timeout=900)
    manifest = {
        "schema": SCHEMA,
        "revision": revision,
        "source_tree": source_tree,
        "run": run,
        "image_ref": image,
        "layers": found["layers"],
        "user": found["user"],
        "tar_sha256": sha256(tar),
        "tar_bytes": tar.stat().st_size,
        "base_images": base_images(dockerfile),
        "tools": tools,
        "materials": recorded,
        "reproducibility": json.loads(materials["reproducibility.json"].read_text()),
        "saved_at": datetime.now(UTC).isoformat(),
    }
    fd = os.open(output / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
    return manifest


def reproducibility(first: str, second: str) -> dict[str, Any]:
    """Compare two builds of the same revision and inputs, layer by layer."""
    a, b = identity(first)["layers"], identity(second)["layers"]
    differing = [
        i
        for i in range(max(len(a), len(b)))
        if i >= len(a) or i >= len(b) or a[i] != b[i]
    ]
    return {
        "method": "second docker build --no-cache from the same checkout and inputs",
        "total_layers": max(len(a), len(b)),
        "equal_layers": max(len(a), len(b)) - len(differing),
        "differing_layer_indexes": differing,
        "identical": not differing,
    }


def check_manifest(manifest: dict[str, Any], revision: str) -> None:
    materials = manifest.get("materials")
    if (
        manifest.get("schema") != SCHEMA
        or manifest.get("revision") != _revision(revision)
        or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("tar_sha256", "")))
        or not isinstance(manifest.get("layers"), list)
        or not manifest["layers"]
        or not isinstance(materials, dict)
        or set(materials) != set(MATERIALS)
        or not all(re.fullmatch(r"[0-9a-f]{64}", str(v)) for v in materials.values())
        or not isinstance(manifest.get("tools"), dict)
        or not manifest["tools"]
        or not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("source_tree", "")))
    ):
        raise ValueError("Release manifest is malformed or for another revision")


def verify_materials(directory: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Every provenance file matches its manifest digest; the licence policy passed."""
    for name, digest in manifest["materials"].items():
        if sha256(directory / name) != digest:
            raise ValueError(f"Provenance material {name} differs from the manifest")
    verdict = json.loads((directory / "license-verdict.json").read_text())
    if verdict.get("passed") is not True:
        raise ValueError("Licence policy did not pass for this artifact")
    sbom = json.loads((directory / "sbom.cdx.json").read_text())
    if sbom.get("bomFormat") != "CycloneDX" or not sbom.get("components"):
        raise ValueError("SBOM is not a populated CycloneDX document")
    return {
        "sbom_components": len(sbom["components"]),
        "licence_findings": verdict.get("findings"),
        "reproducible": manifest["reproducibility"].get("identical") is True,
    }


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
    s.add_argument("--material", action="append", default=[], metavar="NAME=PATH")
    s.add_argument("--tool", action="append", default=[], metavar="NAME=VERSION")
    s.add_argument("--dockerfile", type=Path, required=True)
    s.add_argument("--source-tree", required=True)
    c = sub.add_parser("compare-builds")
    c.add_argument("--first", required=True)
    c.add_argument("--second", required=True)
    c.add_argument("--output", type=Path, required=True)
    v = sub.add_parser("verify")
    v.add_argument("--tar", type=Path, required=True)
    v.add_argument("--manifest", type=Path, required=True)
    v.add_argument("--revision", required=True)
    v.add_argument("--tag", required=True)
    args = parser.parse_args()
    try:
        if args.command == "save":
            result = save(
                args.image,
                args.revision,
                args.output,
                args.run,
                materials={
                    k: Path(v) for k, _, v in (m.partition("=") for m in args.material)
                },
                tools=dict(t.partition("=")[::2] for t in args.tool),
                dockerfile=args.dockerfile,
                source_tree=args.source_tree,
            )
        elif args.command == "compare-builds":
            result = reproducibility(args.first, args.second)
            args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        else:
            result = verify(args.tar, args.manifest, args.revision, args.tag)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"release artifact refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
