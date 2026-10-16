"""Closed retained-source and raw-command bindings for full-core evidence."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import builtins
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
from typing import Any

from . import core_source as source


class InvalidEvidence(ValueError):
    """The retained bundle cannot establish the full core gate."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise InvalidEvidence(message)


def document(path: Path, kind: type = dict) -> Any:
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in rows:
            require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result

    try:
        require(path.is_file() and not path.is_symlink(), f"Missing/linked {path.name}")
        result = json.loads(path.read_text(), object_pairs_hook=pairs)
        require(type(result) is kind, f"Invalid document type: {path.name}")
        return result
    except (OSError, ValueError) as exc:
        if isinstance(exc, InvalidEvidence):
            raise
        raise InvalidEvidence(f"Invalid JSON: {path.name}") from exc


def manifest(output: Path, revision: str, max_age: int) -> dict[str, Any]:
    report = document(output / "manifest.json")
    require(type(max_age) is int and 1 <= max_age <= 30, "Invalid max-age policy")
    require(re.fullmatch(r"[a-f0-9]{40}", revision), "Expected full revision")
    require(
        type(report.get("schema_version")) is int and report["schema_version"] == 1,
        "Invalid schema",
    )
    require(report.get("source_revision") == revision, "Source revision differs")
    for key, value in {
        "certifying": True,
        "passed": True,
        "source_unchanged": True,
        "development": False,
        "cancelled": False,
    }.items():
        require(report.get(key) is value, f"Noncertifying producer: {key}")
    require(
        not any(
            key in report
            for key in (
                "error",
                "error_type",
                "cleanup_error",
                "source_error",
                "evidence_error",
            )
        ),
        "Producer reports failure",
    )
    try:
        start, end = [
            datetime.fromisoformat(report[key]) for key in ("started_at", "finished_at")
        ]
        now = datetime.now(timezone.utc)
        require(
            start.utcoffset() is not None and end.utcoffset() is not None,
            "Naive evidence timestamp",
        )
        require(
            now - timedelta(days=max_age) <= start <= end <= now,
            "Stale/future/unordered evidence",
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidEvidence("Invalid evidence timestamps") from exc
    return report


def bind_source(
    root: Path, output: Path, revision: str, report: dict[str, Any]
) -> None:
    captured = document(output / "source.json")
    require(
        report["source"] == captured and captured["revision"] == revision,
        "Source metadata differs",
    )
    entries = source.tracked(root, revision)
    require(
        source.git(root, "rev-parse", "HEAD") == revision,
        "Requested checkout revision differs",
    )
    require(
        captured["git_tree"] == source.git(root, "rev-parse", revision + "^{tree}"),
        "Git tree differs",
    )
    worktree = captured["worktree"]
    require(
        worktree["dirty"] is False and worktree["status"] == "",
        "Producer source was dirty",
    )
    require(
        source.working_state(root, entries) == worktree,
        "Current source differs or is dirty",
    )
    files = source.inventory(output / "source")
    require(
        files == captured["files"] == worktree["files"] and set(files) == set(entries),
        "Complete source inventory differs",
    )
    require(source.tree_hash(files) == captured["sha256"], "Source tree hash differs")
    require(
        source.sha256(output / "source.tar") == captured["archive_sha256"],
        "Source archive hash differs",
    )
    archived: dict[str, str] = {}
    with tarfile.open(output / "source.tar") as package:
        seen: set[str] = set()
        for member in package:
            pure = PurePosixPath(member.name)
            require(
                not pure.is_absolute()
                and ".." not in pure.parts
                and ".git" not in pure.parts,
                "Unsafe archive path",
            )
            require(member.name not in seen, "Duplicate archive member")
            seen.add(member.name)
            require(member.isdir() or member.isfile(), "Linked/special archive member")
            if member.isfile():
                require(member.name in entries, "Extra archived source")
                stream = package.extractfile(member)
                require(stream is not None, "Missing archived bytes")
                with stream:
                    data = stream.read()
                oid, size = entries[member.name]
                blob = hashlib.sha1(
                    f"blob {size}\0".encode() + data, usedforsecurity=False
                ).hexdigest()
                require(
                    len(data) == size and blob == oid,
                    "Archive differs from committed source",
                )
                archived[member.name] = hashlib.sha256(data).hexdigest()
    require(archived == files, "Archive/snapshot inventory differs")
    expected = {}
    for filename in (
        "core_runner.py",
        "core_source.py",
        "core_resources.py",
        "core_profile.py",
        "python_executor.py",
        "cancellation.py",
    ):
        name = "tools/ci/" + filename
        require(
            source.sha256(Path(__file__).with_name(filename)) == files.get(name),
            "Consumer/producer helper differs from requested source",
        )
        expected[name] = files[name]
    require(report["producer_sha256"] == expected, "Producer helper identities differ")
    require(
        source.sha256(Path(__file__).with_name("core_results.py"))
        == files.get("tools/ci/core_results.py"),
        "Results helper differs from source",
    )


def raw_inventory(output: Path, report: dict[str, Any]) -> dict[str, str]:
    all_files = source.inventory(output)
    expected = {
        name: digest
        for name, digest in all_files.items()
        if not name.startswith(("source/", "docker-config/"))
        and name not in {"source.tar", "manifest.json"}
    }
    require(report.get("raw_sha256") == expected, "Missing/extra/modified raw evidence")
    require(
        not any(name.startswith("docker-config/") for name in all_files),
        "Unexpected Docker credential/config material",
    )
    fixed = {
        "source.json",
        "driver.py",
        "image-identity.json",
        "docker-commands.json",
        "resources.json",
    }
    result_names = {
        "executor-probe.json",
        "validation.json",
        "pytest.log",
        "coverage.json",
        "coverage-recomputed.json",
        ".coverage",
        "junit.xml",
    }
    for name in expected:
        require(
            name in fixed
            or name in {"results/" + item for item in result_names}
            or re.fullmatch(r"docker-[0-9]{3}\.(?:stdout|stderr)", name),
            "Unexpected raw artifact inventory",
        )
    require(
        fixed <= expected.keys()
        and {"results/" + item for item in result_names} <= expected.keys(),
        "Missing required raw artifact inventory",
    )
    return all_files


def commands(output: Path) -> list[dict[str, Any]]:
    rows = document(output / "docker-commands.json", list)
    require(rows, "Missing Docker lifecycle commands")
    streams: set[str] = set()
    for number, row in enumerate(rows):
        require(
            type(row) is dict
            and type(row.get("argv")) is list
            and all(type(x) is str for x in row["argv"]),
            "Invalid raw argv",
        )
        require(
            row["argv"][:1] == ["docker"] and len(row["argv"]) > 1, "Non-Docker command"
        )
        error_name = row.get("error_type")
        builtin_error = (
            getattr(builtins, error_name, None) if type(error_name) is str else None
        )
        recovered_stop = (
            row["argv"][:3] == ["docker", "stop", "--time=3"]
            and len(row["argv"]) == 4
            and row.get("exit_code") is None
            and (
                error_name
                in {"TimeoutExpired", "SubprocessError", "CalledProcessError"}
                or (
                    isinstance(builtin_error, type)
                    and issubclass(builtin_error, OSError)
                )
            )
            and row.get("timeout_seconds") == 15
        )
        require(
            (type(row.get("exit_code")) is int and "error_type" not in row)
            or recovered_stop,
            "Missing real command exit or unsupported exception",
        )
        timeout = row.get("timeout_seconds")
        require(
            type(timeout) in (int, float) and 0 < timeout <= 1800, "Unbounded command"
        )
        for stream in ("stdout", "stderr"):
            name = f"docker-{number:03d}.{stream}"
            require(row.get(stream) == name, "Raw stream identity differs")
            streams.add(name)
            row[stream + "_text"] = (output / name).read_text()
    require(
        streams
        == {
            p.name
            for p in output.glob("docker-*.*")
            if p.name != "docker-commands.json"
        },
        "Extra/missing raw command stream",
    )
    return rows
