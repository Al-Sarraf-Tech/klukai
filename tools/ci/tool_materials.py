"""Exact committed CI-tool materials and reproducible material-history context."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
from typing import Any

from .core_source import SourceFailure, require, sha256
from .python_executor import lock_versions

MATERIALS = (
    "Dockerfile.ci",
    "Dockerfile.ci.dockerignore",
    "requirements-build.txt",
    "requirements-ci.txt",
    "package_runtime.py",
)
PREFIX = "docker/core/"
DIGEST_IMAGE = r"[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}"


def git_bytes(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(root), *args],
        capture_output=True,
        timeout=60,
        check=False,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LC_ALL": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_LAZY_FETCH": "1",
        },
    )
    require(result.returncode == 0, "Material Git history is incomplete or unavailable")
    return result.stdout


def history(root: Path, revision: str) -> dict[str, dict[str, Any]]:
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "Expected full material revision")
    require(
        git_bytes(root, "rev-parse", "--is-shallow-repository").strip() == b"false",
        "Shallow material history cannot certify an epoch",
    )
    require(
        not git_bytes(root, "replace", "-l").strip(),
        "Replaced material history cannot certify",
    )
    grafts = Path(
        git_bytes(
            root, "rev-parse", "--path-format=absolute", "--git-path", "info/grafts"
        )
        .decode()
        .strip()
    )
    require(not grafts.exists(), "Grafted material history cannot certify")
    git_bytes(root, "fsck", "--connectivity-only", "--no-dangling", revision)
    result = {}
    for name in MATERIALS:
        path = PREFIX + name
        raw = (
            git_bytes(root, "log", "-1", "--format=%H%x00%ct", revision, "--", path)
            .decode()
            .strip()
        )
        match = re.fullmatch(r"([a-f0-9]{40})\x00([1-9][0-9]*)", raw)
        require(match is not None, "Missing last-change material commit/timestamp")
        if match is None:
            raise SourceFailure("Unreachable malformed material history")
        oid, epoch = match.groups()
        git_bytes(root, "merge-base", "--is-ancestor", oid, revision)
        content = git_bytes(root, "cat-file", "blob", oid + ":" + path)
        result[path] = {
            "commit": oid,
            "timestamp": int(epoch),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    return result


def validate_hash_lock(text: str, count: int) -> dict[str, str]:
    packages = lock_versions(text)
    require(len(packages) == count, "Unexpected complete package-lock cardinality")
    hashes = 0
    active = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash="):
            require(
                re.fullmatch(r"--hash=sha256:[a-f0-9]{64}\s*\\?", line),
                "Malformed dependency hash",
            )
            hashes += 1
        else:
            require(not active or hashes > 0, "Dependency has no pinned hashes")
            active, hashes = True, 0
    require(active and hashes > 0, "Dependency has no pinned hashes")
    return packages


def recipe_instructions(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Admit an explicit Dockerfile subset under the pinned default lexer.

    Keywords use Docker's case-insensitive horizontal-whitespace boundaries.
    Default backslash continuations are joined before parsing instructions;
    ambiguous escapes, heredocs and extra parser directives fail closed.
    """
    normalized = text.replace("\r\n", "\n")
    require(
        not re.search(r"[\x00-\x08\x0b-\x1f\x7f\ufeff]", normalized),
        "Unsupported Dockerfile control character",
    )
    lines = normalized.split("\n")
    frontend = lines[0].removeprefix("# syntax=")
    require(
        lines[0].startswith("# syntax=") and re.fullmatch(DIGEST_IMAGE, frontend),
        "Build frontend must have an immutable digest default",
    )
    instructions = []
    pending = ""
    allowed = {
        "ARG",
        "FROM",
        "ENV",
        "WORKDIR",
        "COPY",
        "RUN",
        "LABEL",
        "USER",
        "ENTRYPOINT",
        "CMD",
    }
    for raw in lines[1:]:
        line = raw.strip(" \t")
        if not line or line.startswith("#"):
            require(
                not pending,
                "Comments/blank lines within a continuation are not admitted",
            )
            require(
                not re.match(
                    r"#[ \t]*(?:syntax|escape|check)[ \t]*=", line, re.IGNORECASE
                ),
                "Unadmitted Dockerfile parser directive",
            )
            continue
        require("<<" not in line, "Dockerfile heredocs are not admitted")
        if "\\" in line:
            require(
                raw.endswith("\\")
                and line.count("\\") == 1
                and len(line) > 1
                and line[-2] in " \t",
                "Unsupported Dockerfile escape/continuation",
            )
            pending += line[:-1]
            continue
        logical, pending = pending + line, ""
        match = re.fullmatch(r"([A-Za-z]+)[ \t]+(.+)", logical)
        require(match is not None, "Malformed Dockerfile logical instruction")
        if match is None:
            raise SourceFailure("Unreachable malformed Dockerfile instruction")
        opcode, body = match.groups()
        require(opcode.upper() in allowed, "Unadmitted Dockerfile instruction")
        instructions.append((opcode.upper(), body))
    require(not pending, "Dangling Dockerfile continuation")
    return frontend, instructions


def validate_recipe(text: str) -> dict[str, str]:
    frontend, instructions = recipe_instructions(text)
    # FROM expands global ARG defaults. Stage-local ARG declarations must not
    # silently change the validator's interpretation of a later FROM.
    variables: dict[str, str] = {}
    bases: list[str] = []
    for opcode, body in instructions:
        if opcode == "ARG":
            match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?:=(.*))?", body)
            require(match is not None, "Malformed Dockerfile ARG")
            if match is not None and not bases and match[2] is not None:
                variables[match[1]] = match[2]
        elif opcode == "FROM":
            parts = re.split(r"[ \t]+", body)
            require(
                len(parts) == 3
                and parts[1].upper() == "AS"
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", parts[2]),
                "Unadmitted Dockerfile FROM form",
            )
            value = parts[0]
            for key, default in variables.items():
                value = value.replace("${" + key + "}", default)
            require(
                re.fullmatch(DIGEST_IMAGE, value),
                "Build base must have an immutable digest default",
            )
            bases.append(value)
    require(
        len(bases) == 2 and len(set(bases)) == 1,
        "Expected the locked two-stage CPython tool build",
    )
    return {"frontend": frontend, "python_image": bases[0]}


def context_inventory(directory: Path) -> dict[str, str]:
    require(
        directory.is_dir() and not directory.is_symlink(),
        "Missing/linked material context",
    )
    require(
        {path.name for path in directory.iterdir()} == set(MATERIALS),
        "Extra or missing material context entries",
    )
    result = {}
    for name in MATERIALS:
        path = directory / name
        require(
            path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1,
            "Material context contains a link/directory/special file",
        )
        result[name] = sha256(path)
    return result


def archive_context(directory: Path, archive: Path, epoch: int) -> None:
    with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as package:
        for name in sorted(MATERIALS):
            content = (directory / name).read_bytes()
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode, entry.mtime = len(content), 0o644, epoch
            entry.uid = entry.gid = 0
            entry.uname = entry.gname = ""
            package.addfile(entry, io.BytesIO(content))


def prepare(root: Path, output: Path, captured: dict[str, Any]) -> dict[str, Any]:
    changes = history(root, captured["revision"])
    expected = {}
    for name in MATERIALS:
        path = PREFIX + name
        require(path in captured["files"], "Missing committed build material")
        digest = captured["files"][path]
        require(
            changes[path]["sha256"] == digest,
            "Last-change material bytes differ from selected revision",
        )
        expected[name] = digest
    epoch = max(row["timestamp"] for row in changes.values())
    directory = output / "context"
    directory.mkdir(mode=0o700)
    for name in MATERIALS:
        shutil.copyfile(output / "source" / PREFIX / name, directory / name)
        (directory / name).chmod(0o644)
        os.utime(directory / name, (epoch, epoch))
    require(context_inventory(directory) == expected, "Copied material bytes differ")
    recipe = validate_recipe((directory / "Dockerfile.ci").read_text())
    validate_hash_lock((directory / "requirements-build.txt").read_text(), 3)
    packages = validate_hash_lock((directory / "requirements-ci.txt").read_text(), 130)
    archive = output / "context.tar"
    archive_context(directory, archive, epoch)
    return {
        "source_date_epoch": epoch,
        "last_changes": changes,
        "source_sha256": {PREFIX + name: digest for name, digest in expected.items()},
        "context_sha256": expected,
        "archive_sha256": sha256(archive),
        "recipe": recipe,
        "packages": packages,
    }


def unchanged(
    root: Path, output: Path, captured: dict[str, Any], materials: dict[str, Any]
) -> None:
    expected = {name: captured["files"][PREFIX + name] for name in MATERIALS}
    require(
        context_inventory(output / "context")
        == expected
        == materials["context_sha256"],
        "Material context changed during the build",
    )
    epoch = materials["source_date_epoch"]
    require(
        all(
            path.stat().st_mtime_ns == epoch * 1_000_000_000
            and path.stat().st_mode & 0o777 == 0o644
            for path in (output / "context").iterdir()
        ),
        "Material context epoch/mode changed",
    )
    archive = output / "context.tar"
    require(
        archive.is_file() and not archive.is_symlink() and archive.stat().st_nlink == 1,
        "Material archive is missing or linked",
    )
    require(
        sha256(archive) == materials["archive_sha256"],
        "Deterministic context archive changed",
    )
    changes = history(root, captured["revision"])
    require(
        changes == materials["last_changes"],
        "Material history changed during the build",
    )
    require(
        type(materials["source_date_epoch"]) is int
        and materials["source_date_epoch"]
        == max(row["timestamp"] for row in changes.values()),
        "Material source epoch differs from verified history",
    )
