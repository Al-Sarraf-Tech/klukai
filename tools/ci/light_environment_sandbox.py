"""Offline native wheel builds with only selected tool/material mounts."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any, Protocol

from .core_source import require, sha256
from . import light_environment_ca

BWRAP = Path("/usr/bin/bwrap")


class Runner(Protocol):
    def run(self, argv: list[str]) -> str: ...


def identity(runner: Runner) -> dict[str, Any]:
    require(
        BWRAP.is_file() and not BWRAP.is_symlink(),
        "Native bwrap prerequisite missing/linked",
    )
    return {
        "executable": str(BWRAP),
        "sha256": sha256(BWRAP),
        "version": runner.run([str(BWRAP), "--version"]).strip(),
        "capability": False,
    }


def unchanged(record: dict[str, Any]) -> None:
    require(
        record["executable"] == str(BWRAP) and sha256(BWRAP) == record["sha256"],
        "Native bwrap prerequisite changed",
    )


def command(
    builder: Path,
    downloads: Path,
    wheels: Path,
    lock: Path,
    *,
    ca: dict[str, Any] | None = None,
) -> list[str]:
    mounts = [
        (builder, builder, "--ro-bind"),
        (downloads, downloads, "--ro-bind"),
        (wheels, wheels, "--bind"),
        (lock, lock, "--ro-bind"),
    ]
    if ca is not None:
        mounts.extend(
            (source, target, "--ro-bind")
            for source, target in light_environment_ca.mounts(ca, builder)
        )
    prefix = Path(sys.base_prefix).resolve(strict=True)
    if not prefix.is_relative_to("/usr"):
        require(
            prefix != Path("/")
            and not prefix.is_relative_to("/home")
            and not prefix.is_relative_to("/root"),
            "Unsafe native base prefix",
        )
        mounts.append((prefix, prefix, "--ro-bind"))
    args = [
        str(BWRAP),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--ro-bind",
        "/usr",
        "/usr",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
    ]
    for name in ("bin", "sbin", "lib", "lib64"):
        path = Path("/") / name
        if path.exists():
            require(
                path.is_symlink() and path.resolve().is_relative_to("/usr"),
                "Unsupported native library/bin layout",
            )
            args.extend(["--symlink", os.readlink(path), str(path)])
    directories: set[Path] = {Path("/"), Path("/usr"), Path("/tmp"), Path("/run")}
    for path, target, flag in mounts:
        require(
            path.is_absolute()
            and path == path.resolve()
            and not any(p.is_symlink() for p in (path, *path.parents)),
            "Sandbox material is linked or relative",
        )
        for parent in reversed(target.parents):
            if parent not in directories:
                args.extend(["--dir", str(parent)])
                directories.add(parent)
        args.extend([flag, str(path), str(target)])
        directories.add(target)
    cache = Path("/etc/ld.so.cache")
    if cache.is_file():
        if Path("/etc") not in directories:
            args.extend(["--dir", "/etc"])
        args.extend(["--ro-bind", str(cache), str(cache)])
    args.extend(["--chdir", str(wheels), "--dir", "/tmp/home"])
    for key, value in {
        "HOME": "/tmp/home",
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C.UTF-8",
        "PIP_CONFIG_FILE": "/dev/null",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_CACHE_DIR": "1",
        "SOURCE_DATE_EPOCH": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }.items():
        args.extend(["--setenv", key, value])
    return args
