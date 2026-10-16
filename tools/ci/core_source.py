"""Private committed-source snapshots and before/after bindings for core CI."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
from typing import Any


class SourceFailure(ValueError):
    """The source cannot establish a clean, unchanged committed execution."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise SourceFailure(message)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tree_hash(files: dict[str, str]) -> str:
    return hashlib.sha256(
        "".join(f"{name}\0{files[name]}\n" for name in sorted(files)).encode()
    ).hexdigest()


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def reserve_output(root: Path, output: Path) -> None:
    require(
        output.is_absolute() and output == output.resolve(),
        "Output must be absolute and nonlinked",
    )
    require(
        not output.is_relative_to(root.resolve()), "Output must be outside the checkout"
    )
    require(
        not output.exists() and not output.is_symlink(),
        "Output must be a new run directory",
    )
    require(
        not any(path.is_symlink() for path in output.parents), "Output parent is linked"
    )
    output.mkdir(mode=0o700, parents=True)
    output.chmod(0o700)
    require(output.stat().st_uid == os.getuid(), "Output must belong to the executor")


def tracked(root: Path, revision: str) -> dict[str, tuple[str, int]]:
    rows = subprocess.check_output(
        ["git", "-C", str(root), "ls-tree", "-r", "-l", "-z", revision]
    )
    result = {}
    for row in rows.decode().split("\0"):
        if not row:
            continue
        metadata, name = row.split("\t", 1)
        mode, kind, oid, size = metadata.split()
        pure = PurePosixPath(name)
        require(
            mode in {"100644", "100755"} and kind == "blob",
            "Only regular committed files are admitted; no links",
        )
        require(
            not pure.is_absolute()
            and ".." not in pure.parts
            and ".git" not in pure.parts,
            "Unsafe committed source path",
        )
        require(
            pure.name not in {"owner.env", ".env"} and ".mcp-secrets" not in pure.parts,
            "Credential material cannot enter source snapshot",
        )
        result[name] = oid, int(size)
    require(result, "Empty committed source")
    return result


def inventory(directory: Path) -> dict[str, str]:
    files = {}
    for path in sorted(directory.rglob("*")):
        require(not path.is_symlink(), "Source snapshot contains a link")
        if path.is_file():
            files[path.relative_to(directory).as_posix()] = sha256(path)
        else:
            require(path.is_dir(), "Source snapshot contains a special file")
    return files


def working_state(root: Path, names: dict[str, tuple[str, int]]) -> dict[str, Any]:
    files = {}
    for name in names:
        path = root / name
        regular = path.is_file() and not any(
            p.is_symlink() for p in (path, *path.parents)
        )
        files[name] = sha256(path) if regular else "<missing-or-linked>"
    return {
        "head": git(root, "rev-parse", "HEAD"),
        "dirty": bool(git(root, "status", "--porcelain", "--untracked-files=all")),
        "status": git(root, "status", "--porcelain", "--untracked-files=all"),
        "files": files,
    }


def snapshot(
    root: Path, output: Path, revision: str, *, development: bool
) -> dict[str, Any]:
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "Expected full source revision")
    require(git(root, "rev-parse", "HEAD") == revision, "Source revision mismatch")
    entries = tracked(root, revision)
    before = working_state(root, entries)
    require(development or not before["dirty"], "Certifying source must be clean")
    required_bytes = 2 * sum(size for _, size in entries.values()) + 512 * 1024**2
    require(
        shutil.disk_usage(output).free >= required_bytes,
        "Insufficient private snapshot capacity",
    )
    archive = output / "source.tar"
    with archive.open("wb") as stream:
        subprocess.run(
            ["git", "-C", str(root), "archive", "--format=tar", revision],
            stdout=stream,
            check=True,
        )
    destination = output / "source"
    destination.mkdir(mode=0o700)
    with tarfile.open(archive) as package:
        for member in package.getmembers():
            pure = PurePosixPath(member.name)
            require(
                not pure.is_absolute()
                and ".." not in pure.parts
                and ".git" not in pure.parts,
                "Unsafe archive path",
            )
            require(
                member.isdir() or member.isfile(),
                "Archive contains nonregular/link material",
            )
        package.extractall(destination, filter="data")
    files = inventory(destination)
    require(
        set(files) == set(entries), "Archive differs from complete committed inventory"
    )
    for name, (oid, size) in entries.items():
        path = destination / name
        blob = hashlib.sha1(f"blob {size}\0".encode(), usedforsecurity=False)
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                blob.update(chunk)
        require(
            path.stat().st_size == size and blob.hexdigest() == oid,
            "Archived bytes differ from Git blob",
        )
    result = {
        "revision": revision,
        "git_tree": git(root, "rev-parse", revision + "^{tree}"),
        "files": files,
        "sha256": tree_hash(files),
        "archive_sha256": sha256(archive),
        "worktree": before,
    }
    write_json(output / "source.json", result)
    unchanged(root, output, result)
    return result


def unchanged(root: Path, output: Path, captured: dict[str, Any]) -> None:
    entries = tracked(root, captured["revision"])
    require(
        working_state(root, entries) == captured["worktree"],
        "Working source changed during execution",
    )
    require(
        inventory(output / "source") == captured["files"],
        "Read-only source snapshot changed during execution",
    )
    require(
        sha256(output / "source.tar") == captured["archive_sha256"],
        "Source archive changed during execution",
    )
