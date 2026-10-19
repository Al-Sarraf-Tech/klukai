#!/usr/bin/env python3
"""Build the Klukai CI base image on amarillo (root; run with klukai-ci-runner stopped).

    sudo python3 ops/amarillo/ci-runner/build-base.py --toolchain-dir ~/.local/share/klukai/ci-base-src

Boots the pinned Ubuntu 24.04 cloud image as a throwaway builder VM on the
firewalled klukai-ci network, runs guest/provision.sh from a read-only share,
checks its success marker, then flattens the result into
/var/lib/libvirt/images/klukai-ci/base/base.qcow2 with its SHA-256 and a
manifest of every input digest and installed version. Job VMs only ever get a
copy-on-write overlay of this file, so it must not change while one runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import klukai_ci_runner as kci  # noqa: E402

UPSTREAM = kci.ROOT / "upstream" / "ubuntu-24.04-20260926.img"
UPSTREAM_SHA256 = "6a81c37564db9b1ee84e141922625e1d7c5b389b99bb3c572e0243607d5bb4d2"
RUNNER_URL = (
    "https://github.com/actions/runner/releases/download/v2.338.0/"
    "actions-runner-linux-x64-2.338.0.tar.gz"
)
RUNNER_SHA256 = "af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32"
TOOLCHAIN = {
    "flutter-owner-snapshot.tar.gz": "3bfe5ee4e4b7eeccb0abdeb2d9ae35d1c6b976c630ef252a4f50525a5a957a57",
    "chrome-owner-snapshot.tar.gz": "a0fe81c6f95941ea000cf0bee5d07d844c84aae71fe5431ca6ed91823cfdff9c",
}
GUEST_FILES = (
    "provision.sh",
    "klukai-ci-job",
    "klukai-ci-job.service",
    "job-started.sh",
    "fstab.append",
    "klukai-ci-chrome-b838d34bba223857",
)
BUILDER = "klukai-ci-build"  # outside the controller's klukai-ci-<hex> namespace
BUILD = kci.ROOT / "build"
BUILD_PROFILE = kci.Profile("build", 8, 8192)
SLOT = 3
MARKER = "KLUKAI-BASE-OK"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def run(*argv: str, timeout: float = 600) -> str:
    return subprocess.run(
        argv, check=True, capture_output=True, text=True, timeout=timeout
    ).stdout


def verified(path: Path, expected: str) -> Path:
    if sha256(path) != expected:
        raise SystemExit(f"digest mismatch: {path}")
    return path


def builder_xml(build: Path) -> str:
    """The job VM definition with the build shares instead of the evidence tree."""
    root = ET.fromstring(
        kci.domain_xml("klukai-ci-000000000000", BUILD_PROFILE, SLOT, build)
    )
    root.find("name").text = BUILDER
    root.find("devices/serial/source").set(
        "path", str(kci.CONSOLE_LOGS / f"{BUILDER}.console.log")
    )
    devices = root.find("devices")
    devices.remove(devices.find("filesystem"))
    for tag, source, readonly in (
        ("build-inputs", build / "inputs", True),
        ("build-out", build / "out", False),
    ):
        share = ET.SubElement(
            devices, "filesystem", type="mount", accessmode="passthrough"
        )
        ET.SubElement(share, "driver", type="virtiofs")
        ET.SubElement(share, "source", dir=str(source))
        ET.SubElement(share, "target", dir=tag)
        if readonly:
            ET.SubElement(share, "readonly")
    return ET.tostring(root, encoding="unicode")


def user_data() -> str:
    return "#cloud-config\n" + json.dumps(
        {
            "users": [],
            "mounts": [
                [
                    "build-inputs",
                    "/mnt/build-inputs",
                    "virtiofs",
                    "ro,nofail",
                    "0",
                    "0",
                ],
                ["build-out", "/mnt/build-out", "virtiofs", "rw,nofail", "0", "0"],
            ],
            "runcmd": [
                [
                    "sh",
                    "-c",
                    "bash /mnt/build-inputs/provision.sh || echo '<2>KLUKAI-BASE-FAILED' > /dev/kmsg; "
                    "umount /mnt/build-out /mnt/build-inputs; poweroff",
                ]
            ],
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--toolchain-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    if any(kci.NAME_RE.fullmatch(n) for n in run(*kci.VIRSH, "list", "--name").split()):
        raise SystemExit("a Klukai CI job VM is running; stop klukai-ci-runner first")
    verified(UPSTREAM, UPSTREAM_SHA256)

    shutil.rmtree(BUILD, ignore_errors=True)
    inputs, out = BUILD / "inputs", BUILD / "out"
    for directory in (BUILD, inputs, out):
        directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        shutil.chown(directory, "root", "qemu")
    for name in GUEST_FILES:
        shutil.copy2(HERE / "guest" / name, inputs / name)
    for name, digest in TOOLCHAIN.items():
        shutil.copy2(verified(args.toolchain_dir / name, digest), inputs / name)
    runner = inputs / "actions-runner-linux-x64-2.338.0.tar.gz"
    with (
        urllib.request.urlopen(RUNNER_URL, timeout=120) as response,
        runner.open("wb") as dest,
    ):
        shutil.copyfileobj(response, dest)
    verified(runner, RUNNER_SHA256)

    run(
        "qemu-img",
        "create",
        "-q",
        "-f",
        "qcow2",
        "-F",
        "qcow2",
        "-b",
        str(UPSTREAM),
        str(BUILD / "disk.qcow2"),
        "40G",
    )
    seed = BUILD / "seed-src"
    seed.mkdir()
    (seed / "user-data").write_text(user_data())
    (seed / "meta-data").write_text(
        json.dumps({"instance-id": BUILDER, "local-hostname": BUILDER})
    )
    (seed / "network-config").write_text(kci.network_config(SLOT))
    run(
        "genisoimage",
        "-quiet",
        "-output",
        str(BUILD / "seed.iso"),
        "-volid",
        "cidata",
        "-joliet",
        "-rock",
        *(str(seed / n) for n in ("user-data", "meta-data", "network-config")),
    )
    (BUILD / "domain.xml").write_text(builder_xml(BUILD))
    console = kci.CONSOLE_LOGS / f"{BUILDER}.console.log"
    console.unlink(missing_ok=True)
    run(*kci.VIRSH, "create", str(BUILD / "domain.xml"))
    deadline = time.time() + args.timeout
    while run(*kci.VIRSH, "list", "--name").split().count(BUILDER):
        if time.time() > deadline:
            subprocess.run([*kci.VIRSH, "destroy", BUILDER])
            raise SystemExit("builder timed out")
        time.sleep(10)
    log = console.read_text(errors="replace") if console.exists() else ""
    if MARKER not in log:
        raise SystemExit(
            f"provisioning failed; see {out / 'provision.log'} and {console}"
        )

    base_dir = kci.BASE.parent
    staged = base_dir / "base.qcow2.new"
    run(
        "qemu-img",
        "convert",
        "-O",
        "qcow2",
        str(BUILD / "disk.qcow2"),
        str(staged),
        timeout=3600,
    )
    digest = sha256(staged)
    manifest = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sha256": digest,
        "upstream": {"path": str(UPSTREAM), "sha256": UPSTREAM_SHA256},
        "runner": {"url": RUNNER_URL, "sha256": RUNNER_SHA256},
        "toolchain": TOOLCHAIN,
        "guest_files": {n: sha256(inputs / n) for n in GUEST_FILES},
        "versions": dict(
            line.split("=", 1)
            for line in (out / "versions.txt").read_text().splitlines()
            if "=" in line
        ),
        "images": (out / "images.txt").read_text().split(),
    }
    shutil.copy2(out / "packages.tsv", base_dir / "packages.tsv")
    (base_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    staged.replace(kci.BASE)
    shutil.chown(kci.BASE, "root", "qemu")
    kci.BASE.chmod(0o440)
    kci.BASE_DIGEST.write_text(digest + "\n")
    shutil.rmtree(BUILD, ignore_errors=True)
    console.unlink(missing_ok=True)
    print(
        json.dumps(
            {"base": str(kci.BASE), "sha256": digest, "versions": manifest["versions"]},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
