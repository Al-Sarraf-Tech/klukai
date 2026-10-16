"""Bind a completed venv's files, links, executable targets and metadata."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
from typing import Any

from .core_source import require, sha256


def metadata(info: os.stat_result) -> dict[str, int]:
    return {
        name: getattr(info, "st_" + name)
        for name in (
            "dev",
            "ino",
            "mode",
            "uid",
            "gid",
            "nlink",
            "size",
            "mtime_ns",
            "ctime_ns",
        )
    }


def regular(path: Path) -> dict[str, Any]:
    before = path.stat()
    require(stat.S_ISREG(before.st_mode), "Installed target is not a regular file")
    result = {"metadata": metadata(before), "sha256": sha256(path)}
    require(
        metadata(path.stat()) == result["metadata"],
        "Installed file changed during hashing",
    )
    return result


def capture(directory: Path, host: Path) -> dict[str, Any]:
    require(
        directory.is_dir()
        and directory == directory.resolve()
        and not any(p.is_symlink() for p in (directory, *directory.parents)),
        "Installed environment path is linked/missing",
    )
    interpreter = directory / "bin/python"
    require(
        interpreter.is_file() and os.access(interpreter, os.X_OK),
        "Installed interpreter is missing or not executable",
    )
    result: dict[str, Any] = {}
    for path in (directory, *sorted(directory.rglob("*"))):
        name = path.relative_to(directory).as_posix()
        info = path.lstat()
        row: dict[str, Any] = {"metadata": metadata(info)}
        if stat.S_ISLNK(info.st_mode):
            target = path.resolve(strict=True)
            row["link"] = os.readlink(path)
            row["target"] = str(target)
            if not target.is_relative_to(directory):
                require(
                    path.parent == directory / "bin"
                    and (
                        re.fullmatch(
                            r"python(?:\d+(?:\.\d+)?)?", path.name, flags=re.ASCII
                        )
                        or path.name == "\U0001d70bthon"
                    )
                    and target == host,
                    "Installed link escapes the venv",
                )
            if target.is_file():
                row["target_file"] = regular(target)
            else:
                require(target.is_dir(), "Invalid installed link target")
        elif stat.S_ISREG(info.st_mode):
            require(info.st_nlink == 1, "Installed file has an external hard link")
            row.update(regular(path))
        else:
            require(
                stat.S_ISDIR(info.st_mode),
                "Installed environment contains a special file",
            )
        require(
            metadata(path.lstat()) == row["metadata"],
            "Installed entry changed during capture",
        )
        result[name] = row
    return result


def unchanged(directory: Path, host: Path, expected: Any) -> None:
    require(
        isinstance(expected, dict) and capture(directory, host) == expected,
        "Installed environment bytes, links or metadata changed",
    )
