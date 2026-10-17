"""Strict parsing and complete current Git/source bindings for PERF-1."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import tarfile
from typing import Any

from . import core_source as source
from .perf_schema import digest


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def loads(raw: str) -> Any:
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in rows:
            require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValueError("Nonfinite JSON constant: " + value)

    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)

    def finite(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                finite(child)
        elif isinstance(value, list):
            for child in value:
                finite(child)
        elif isinstance(value, float):
            require(math.isfinite(value), "Nonfinite JSON number")

    finite(result)
    return result


def document(path: Path, kind: type = dict) -> Any:
    require(
        path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1,
        "Missing/linked evidence: " + path.name,
    )
    result = loads(path.read_text())
    require(type(result) is kind, "Invalid document: " + path.name)
    return result


def helpers(root: Path) -> dict[str, str]:
    names = sorted(path.name for path in (root / "tools/ci").glob("perf_*.py"))
    names += ["core_source.py", "cancellation.py"]
    result = {}
    for name in names:
        expected, executing = root / "tools/ci" / name, Path(__file__).with_name(name)
        require(
            source.sha256(expected) == source.sha256(executing),
            "Executing helper differs: " + name,
        )
        result["tools/ci/" + name] = source.sha256(expected)
    return result


def manifest(output: Path, revision: str, max_age: int) -> dict[str, Any]:
    require(type(max_age) is int and 1 <= max_age <= 30, "Invalid max-age policy")
    require(re.fullmatch(r"[a-f0-9]{40}", revision), "Expected full candidate revision")
    require(
        output.is_absolute()
        and output == output.resolve()
        and not any(p.is_symlink() for p in output.parents),
        "Linked/relative evidence directory",
    )
    report = document(output / "manifest.json")
    require(
        type(report.get("schema_version")) is int
        and report["schema_version"] == 1
        and report.get("source_revision") == revision,
        "Invalid PERF producer identity",
    )
    for name, value in {
        "certifying": True,
        "passed": True,
        "source_unchanged": True,
        "development": False,
    }.items():
        require(report.get(name) is value, "Noncertifying producer: " + name)
    require(
        not any(key.endswith("error") or key == "error_type" for key in report),
        "Failed producer",
    )
    timestamps(report, max_seconds=min(86400, max_age * 86400))
    return report


def timestamps(
    row: dict[str, Any], *, max_seconds: int = 86400
) -> tuple[datetime, datetime]:
    start, end = (
        datetime.fromisoformat(row[key]) for key in ("started_at", "finished_at")
    )
    now = datetime.now(UTC)
    require(
        start.utcoffset() is not None
        and end.utcoffset() is not None
        and now - timedelta(seconds=max_seconds) <= start <= end <= now,
        "Stale/future/unordered performance timestamps",
    )
    return start, end


def bind_source(
    root: Path, output: Path, revision: str, report: dict[str, Any]
) -> None:
    captured = document(output / "source.json")
    require(
        digest(report["source"]) == digest(captured)
        and captured["revision"] == revision,
        "Source metadata differs",
    )
    entries = source.tracked(root, revision)
    require(
        source.git(root, "rev-parse", "HEAD") == revision,
        "Candidate is not current source",
    )
    require(
        captured["git_tree"] == source.git(root, "rev-parse", revision + "^{tree}"),
        "Git tree differs",
    )
    state = source.working_state(root, entries)
    require(
        digest(state) == digest(captured["worktree"])
        and state["dirty"] is False
        and state["status"] == "",
        "Current/producer source is dirty or changed",
    )
    files = source.inventory(output / "source")
    require(
        files == captured["files"] == state["files"] and set(files) == set(entries),
        "Source inventory differs",
    )
    require(
        source.tree_hash(files) == captured["sha256"]
        and source.sha256(output / "source.tar") == captured["archive_sha256"],
        "Source digest differs",
    )
    canonical = subprocess.run(
        ["git", "-C", str(root), "archive", "--format=tar", revision],
        capture_output=True,
        check=True,
        timeout=50,
    ).stdout
    require(
        hashlib.sha256(canonical).hexdigest() == captured["archive_sha256"],
        "Archive bytes differ from canonical Git archive",
    )
    archived = {}
    directories = set()
    seen = set()
    with tarfile.open(output / "source.tar") as archive:
        for member in archive:
            require(
                member.name not in seen and (member.isdir() or member.isfile()),
                "Duplicate/linked archive member",
            )
            seen.add(member.name)
            if member.isdir():
                directories.add(member.name.rstrip("/"))
            require(
                not member.name.startswith("/") and ".." not in Path(member.name).parts,
                "Unsafe archive path",
            )
            if member.isfile():
                require(member.name in entries, "Unknown archived file")
                stream = archive.extractfile(member)
                require(stream is not None, "Missing archived bytes")
                with stream:
                    data = stream.read()
                oid, size = entries[member.name]
                require(
                    len(data) == size
                    and hashlib.sha1(
                        f"blob {size}\0".encode() + data, usedforsecurity=False
                    ).hexdigest()
                    == oid,
                    "Archive differs from Git blob",
                )
                archived[member.name] = hashlib.sha256(data).hexdigest()
    require(archived == files, "Archive/snapshot content differs")
    require(
        directories
        == {
            p.relative_to(output / "source").as_posix()
            for p in (output / "source").rglob("*")
            if p.is_dir()
        },
        "Source snapshot directory inventory differs",
    )
    require(
        report["producer_sha256"] == helpers(root),
        "Producer/consumer helper identities differ",
    )


ANY_WARNING = re.compile(r"\b\w*warning\b|never awaited|Exception ignored", re.I)
PYTHON_WARNINGS = re.compile(r"\b[A-Z]\w*Warning\b|never awaited|Exception ignored")


def raw_inventory(
    output: Path, report: dict[str, Any], rounds: int, prefix: str
) -> None:
    actual = source.inventory(output)
    for path in output.rglob("*"):
        require(not path.is_file() or path.stat().st_nlink == 1, "Hardlinked evidence")
    raw = {
        name: value
        for name, value in actual.items()
        if not name.startswith("source/")
        and name not in {"source.tar", "manifest.json"}
    }
    require(raw == report["raw_sha256"], "Missing/extra/changed raw artifacts")
    fixed = {
        "source.json",
        "inputs.json",
        "harness-sources.json",
        "baseline.json",
        "candidate.json",
        "gate.json",
        "cleanup.json",
        "docker-commands.json",
    }
    fixed |= {
        f"{role}-round-{index}.json"
        for role in ("baseline", "candidate")
        for index in range(rounds)
    }
    fixed |= {
        f"{prefix}-{role}-{index}.log"
        for role in ("baseline", "candidate")
        for index in range(rounds)
    }
    fixed |= {
        f"{prefix}-{role}.log" for role in ("postgres", "redis", "qdrant", "fixture")
    }
    require(fixed <= raw.keys(), "Missing raw producer files")
    require(
        all(
            name in fixed or re.fullmatch(r"docker-[0-9]{3,}\.(stdout|stderr)", name)
            for name in raw
        ),
        "Unknown raw file",
    )
    require(
        not any((output / "docker-config").iterdir()),
        "Docker credentials unexpectedly retained",
    )
    allowed_dirs = {"source", "docker-config"} | {
        str(Path("source") / p.relative_to(output / "source"))
        for p in (output / "source").rglob("*")
        if p.is_dir()
    }
    require(
        all(
            p.relative_to(output).as_posix() in allowed_dirs
            for p in output.rglob("*")
            if p.is_dir()
        ),
        "Unknown evidence directory",
    )


def commands(output: Path) -> list[dict[str, Any]]:
    rows = document(output / "docker-commands.json", list)
    require(rows, "Empty Docker transcript")
    streams = set()
    for index, row in enumerate(rows):
        require(
            type(row) is dict
            and type(row.get("argv")) is list
            and all(type(value) is str for value in row["argv"])
            and row["argv"][:1] == ["docker"],
            "Invalid Docker argv",
        )
        require(
            type(row.get("exit_code")) is int
            and "error_type" not in row
            and row.get("timeout_seconds") == 50,
            "Failed/unbounded Docker command",
        )
        require(
            type(row.get("environment")) is str
            and type(loads(row["environment"])) is dict
            and (row.get("stdin") is None or type(row["stdin"]) is str),
            "Invalid command inputs",
        )
        for stream in ("stdout", "stderr"):
            filename = f"docker-{index:03d}.{stream}"
            require(row.get(stream) == filename, "Raw stream identity differs")
            streams.add(filename)
            row[stream + "_text"] = (output / filename).read_text()
            # Container logs carry log-level WARNING lines from the app's
            # seed step and the disposable backends' tmpfs/locale setup; only
            # Python warning signatures fail there. Docker CLI warnings (for
            # example an unenforced resource limit) always fail.
            pattern = PYTHON_WARNINGS if row["argv"][1:2] == ["logs"] else ANY_WARNING
            require(
                not pattern.search(row[stream + "_text"]),
                "Warnings in actual Docker output",
            )
    require(
        streams
        == {
            p.name
            for p in output.glob("docker-*.*")
            if p.name != "docker-commands.json"
        },
        "Orphan raw stream",
    )
    return rows


def fingerprints(
    root: Path, backends: dict[str, str], policy: dict[str, Any]
) -> dict[str, str]:
    sources = {
        file.name: file.read_text()
        for file in sorted((root / "tools/ci").glob("perf_*.py"))
    }
    # Read the constant from the executing module, whose complete bytes are bound above.
    from .perf_seed import SEED_VERSION

    return {
        "harness": digest(sources),
        "fixture": digest(
            {
                "script": sources["perf_fixtures.py"],
                "personality": (root / "config/personality.yaml").read_text(),
                "backends": backends,
            }
        ),
        "seed": digest(SEED_VERSION),
        "policy": digest(policy),
    }
