"""Optional Docker transport cache; never a replacement for source certification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def regular(path: Path) -> None:
    if (
        not path.is_absolute()
        or not path.is_file()
        or any(p.is_symlink() for p in (path, *path.parents))
    ):
        raise ValueError("Missing, relative or linked cache file")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def publish(record: Path, archive: Path, image: str) -> None:
    regular(archive)
    if not IMAGE.fullmatch(image):
        raise ValueError("Cache image must be immutable")
    if not record.is_absolute() or any(
        p.is_symlink() for p in (record, *record.parents)
    ):
        raise ValueError("Invalid cache record path")
    record.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"archive": str(archive), "sha256": digest(archive), "image": image}
    fd, filename = tempfile.mkstemp(dir=record.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream)
        os.replace(filename, record)
    finally:
        Path(filename).unlink(missing_ok=True)


def read(record: Path) -> tuple[Path, str]:
    regular(record)
    data = json.loads(record.read_text())
    archive, image = Path(data["archive"]), data["image"]
    regular(archive)
    if not IMAGE.fullmatch(image) or digest(archive) != data["sha256"]:
        raise ValueError("Cache image or archive digest differs")
    return archive, image


def restore(record: Path) -> str | None:
    try:
        archive, image = read(record)
        # Published archives live in immutable run-specific directories. A new
        # publisher atomically replaces only the small pointer, never that tar.
        subprocess.run(
            ["docker", "load", "--quiet", "--input", str(archive)],
            check=True,
            capture_output=True,
            timeout=900,
        )
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.stdout.strip() != image:
            raise ValueError("Loaded cache image identity differs")
        return image
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        # Corruption or eviction costs a cold build, never a passing gate.
        return None


def buildkit_inventory(directory: Path) -> dict[str, str]:
    regular(directory / "index.json")
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Linked BuildKit cache entry")
        if path.is_file():
            regular(path)
            value = digest(path)
            if path.parent == directory / "blobs/sha256" and path.name != value:
                raise ValueError("BuildKit blob digest differs")
            result[path.relative_to(directory).as_posix()] = value
        elif not path.is_dir():
            raise ValueError("Special BuildKit cache entry")
    return result


def publish_buildkit(record: Path, directory: Path) -> None:
    inventory = buildkit_inventory(directory)
    if not record.is_absolute() or any(
        p.is_symlink() for p in (record, *record.parents)
    ):
        raise ValueError("Invalid BuildKit cache record")
    record.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(dir=record.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"directory": str(directory), "inventory": inventory}, stream)
        os.replace(filename, record)
    finally:
        Path(filename).unlink(missing_ok=True)


def read_buildkit(record: Path) -> Path:
    regular(record)
    payload = json.loads(record.read_text())
    directory = Path(payload["directory"])
    if buildkit_inventory(directory) != payload["inventory"]:
        raise ValueError("BuildKit cache inventory differs")
    return directory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=("restore", "publish", "restore-buildkit", "publish-buildkit"),
    )
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--image")
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    if args.operation == "publish-buildkit":
        if args.directory is None:
            parser.error("publish-buildkit requires --directory")
        publish_buildkit(args.record, args.directory)
    elif args.operation == "restore-buildkit":
        try:
            print(read_buildkit(args.record))
        except (OSError, ValueError, KeyError, TypeError):
            print("")
    elif args.operation == "publish":
        if args.archive is None or args.image is None:
            parser.error("publish requires --archive and --image")
        publish(args.record, args.archive, args.image)
    else:
        print(restore(args.record) or "")


if __name__ == "__main__":
    main()
