"""Mirror pinned fallback fonts with verified shared caching and bounded fetches."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.error import URLError
from urllib.request import urlopen

FONT_PATH = r"[a-z0-9]+/v[0-9]+/[A-Za-z0-9_-]+(?:\.[0-9]+)?\.(?:woff2|ttf|otf)"
CDN = "https://fonts.gstatic.com/s/"


def safe_path(path: Path) -> Path:
    """A shared cache must not redirect reads or writes through links."""
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError(f"Font path contains a symlink: {parent}")
    return path


def verified(data: bytes, expected: dict) -> bool:
    return (
        len(data) == expected["size"] and sha256(data).hexdigest() == expected["sha256"]
    )


def atomic_write(path: Path, data: bytes) -> None:
    safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".font-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def download(name: str, size: int) -> bytes:
    for attempt in range(3):
        try:
            with urlopen(CDN + name, timeout=20) as response:
                return response.read(size + 1)
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
    raise RuntimeError("Font download did not finish")


def mirror(
    source: Path, cache: Path, output: Path, manifest: Path, *, workers: int = 8
) -> dict:
    if not 1 <= workers <= 16:
        raise ValueError("Font fetch workers must be between 1 and 16")
    lock = json.loads(manifest.read_text())
    if lock.get("schema") != 1 or not isinstance(lock.get("files"), dict):
        raise ValueError("Invalid font manifest")
    files = lock["files"]
    for name, info in files.items():
        if (
            not re.fullmatch(FONT_PATH, name)
            or not isinstance(info, dict)
            or set(info) != {"sha256", "size"}
            or not isinstance(info["sha256"], str)
            or not re.fullmatch("[0-9a-f]{64}", info["sha256"])
            or type(info["size"]) is not int
            or not 0 < info["size"] <= 16 * 1024**2
        ):
            raise ValueError("Invalid pinned font entry")
    names = sorted(set(re.findall('"(' + FONT_PATH + ')"', source.read_text())))
    if not names:
        raise ValueError("No fallback fonts found in built JavaScript")
    if set(names) - files.keys():
        raise ValueError(
            "Build requests unpinned fallback fonts; update the reviewed font manifest"
        )
    safe_path(cache)
    safe_path(output)

    def obtain(name: str) -> tuple[str, bytes, bool]:
        path = safe_path(cache / name)
        try:
            with path.open("rb") as stream:
                data = stream.read(files[name]["size"] + 1)
            if verified(data, files[name]):
                return name, data, False
        except FileNotFoundError:
            pass
        data = download(name, files[name]["size"])
        if not verified(data, files[name]):
            raise ValueError(f"Font digest/size mismatch: {name}")
        atomic_write(path, data)
        return name, data, True

    with ThreadPoolExecutor(max_workers=workers) as executor:
        obtained = list(executor.map(obtain, names))
    # Copy the already-verified bytes, never re-read mutable shared files.
    # Publish only after the complete requested font set has been verified.
    for name, data, _ in obtained:
        atomic_write(output / name, data)
    return {"files": len(names), "downloaded": sum(row[2] for row in obtained)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    try:
        report = mirror(
            args.source, args.cache, args.output, args.manifest, workers=args.workers
        )
    except (OSError, ValueError, URLError) as error:
        print(f"Fallback fonts refused: {error}")
        return 1
    print(
        f"Mirrored {report['files']} verified fallback fonts; {report['downloaded']} downloaded"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
