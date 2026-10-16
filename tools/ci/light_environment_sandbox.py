"""Offline native wheel builds with only selected tool/material mounts."""

from __future__ import annotations

import os
import json
from pathlib import Path
import sys
import sysconfig
from typing import Any, Protocol

from .core_source import require, sha256
from . import light_environment_ca, light_environment_init

BWRAP = Path("/usr/bin/bwrap")
INIT_TARGET = Path("/run/klukai-native-init.py")
INIT_SHA256 = sha256(Path(light_environment_init.__file__))


def init_material() -> dict[str, Any]:
    require(
        sys.implementation.name == "cpython"
        and sys.version_info[0] == 3
        and sys.version_info[1] in {12, 13, 14},
        "Native init requires CPython 3.12 through 3.14",
    )
    path = Path(light_environment_init.__file__).absolute()
    executable = getattr(sys, "_base_executable", "")
    require(isinstance(executable, str) and executable, "Missing base CPython")
    python = Path(executable).resolve(strict=True)
    prefix = Path(sys.base_prefix).resolve(strict=True)
    require(
        path.is_file()
        and path.stat().st_nlink == 1
        and not any(p.is_symlink() for p in (path, *path.parents)),
        "Native init is missing or linked",
    )
    require(
        sha256(path) == INIT_SHA256, "Native init differs from imported helper bytes"
    )
    require(
        python.is_file()
        and os.access(python, os.X_OK)
        and python.is_relative_to(prefix)
        and prefix != Path("/")
        and not prefix.is_relative_to("/home")
        and not prefix.is_relative_to("/root"),
        "Unsafe base CPython location",
    )
    return {
        "path": str(path),
        "sha256": sha256(path),
        "python": str(python),
        "python_sha256": sha256(python),
        "abi": [list(sys.version_info[:2]), sysconfig.get_config_var("SOABI")],
    }


class Runner(Protocol):
    def run(self, argv: list[str]) -> str: ...


def identity(runner: Runner) -> dict[str, Any]:
    require(
        BWRAP.is_file() and not BWRAP.is_symlink(),
        "Native bwrap prerequisite missing/linked",
    )
    material = init_material()
    probe = runner.run(
        [
            material["python"],
            "-I",
            "-c",
            "import json,sys,sysconfig;print(json.dumps([list(sys.version_info[:2]),sysconfig.get_config_var('SOABI')]))",
        ]
    )
    require(json.loads(probe) == material["abi"], "Base CPython ABI differs")
    return {
        "executable": str(BWRAP),
        "sha256": sha256(BWRAP),
        "version": runner.run([str(BWRAP), "--version"]).strip(),
        "capability": False,
        "init": material,
    }


def unchanged(record: dict[str, Any]) -> None:
    require(
        record["executable"] == str(BWRAP) and sha256(BWRAP) == record["sha256"],
        "Native bwrap prerequisite changed",
    )
    require(record.get("init") == init_material(), "Native init/interpreter changed")


def command(
    builder: Path,
    downloads: Path,
    wheels: Path,
    lock: Path,
    *,
    ca: dict[str, Any] | None = None,
) -> list[str]:
    material = init_material()
    mounts = [
        (builder, builder, "--ro-bind"),
        (downloads, downloads, "--ro-bind"),
        (wheels, wheels, "--bind"),
        (lock, lock, "--ro-bind"),
        (Path(material["path"]), INIT_TARGET, "--ro-bind"),
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
        "--as-pid-1",
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
    args.extend(["--", material["python"], "-I", "-B", str(INIT_TARGET)])
    return args
