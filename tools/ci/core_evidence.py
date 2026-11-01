"""Independently consume full managed core-runner evidence; execute no artifacts."""

from __future__ import annotations

import argparse
import json
import importlib.metadata
import tarfile
from pathlib import Path
import re
import sys
from typing import Any

from .core_evidence_identity import (
    InvalidEvidence,
    bind_source,
    commands,
    document,
    manifest,
    raw_inventory,
    require,
)
from .core_profile import DRIVER
from .core_resources import ResourceFailure
from .core_evidence_lifecycle import lifecycle, one
from .core_results import InvalidResults, junit_results, retained_results
from .core_source import SourceFailure, inventory, sha256
from .python_executor import (
    InvalidExecutor,
    lock_versions,
    validate_image,
    validate_probe,
)


def validate(
    root: Path, output: Path, revision: str, max_age_days: int = 30
) -> dict[str, Any]:
    try:
        report = manifest(output, revision, max_age_days)
        require(
            output == output.resolve() and root == root.resolve(),
            "Linked/relative evidence or source root",
        )
        before = raw_inventory(output, report)
        bind_source(root, output, revision, report)
        lock = root / "docker/core/requirements-ci.txt"
        require(report["lock_sha256"] == sha256(lock), "Lock identity differs")
        packages = lock_versions(lock.read_text())
        require(len(packages) == 131, "Expected complete exact131 lock")
        require(
            importlib.metadata.version("coverage") == packages["coverage"],
            "Consumer coverage library differs from pinned lock",
        )
        identity = document(output / "image-identity.json")
        for role in ("core", "postgres"):
            image = report["images"][role]
            require(
                identity[role]["Id"] == image["id"],
                "Retained actual image identity differs",
            )
            reference = image["input"]
            require(
                reference == image["id"]
                or (
                    re.fullmatch(
                        r"[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}", reference
                    )
                    and reference in identity[role]["RepoDigests"]
                ),
                "Image reference is not immutable/bound",
            )
        validate_image(
            identity["core"], report["images"]["core"]["id"], revision, sha256(lock)
        )
        require(
            identity["postgres"].get("PG_MAJOR") == "17"
            and identity["postgres"]["Architecture"] == "amd64"
            and identity["postgres"]["Os"] == "linux",
            "PostgreSQL image identity/major differs",
        )
        rows = commands(output)
        version = rows[one(rows, ["version", "--format={{json .}}"])]
        require(
            version["exit_code"] == 0
            and json.loads(version["stdout_text"]) == report["docker_version"],
            "Actual Docker version output differs",
        )
        require(
            all(
                row["argv"][1]
                in {
                    "image",
                    "version",
                    "network",
                    "create",
                    "start",
                    "exec",
                    "inspect",
                    "stop",
                    "kill",
                    "wait",
                    "rm",
                }
                for row in rows
            ),
            "Unexpected producer command",
        )
        for role in ("core", "postgres"):
            inspected = one(rows, ["image", "inspect", report["images"][role]["input"]])
            require(
                rows[inspected]["exit_code"] == 0
                and re.fullmatch(
                    r"<private inspection bytes sha256=[a-f0-9]{64}>\n",
                    rows[inspected]["stdout_text"],
                ),
                "Original private image inspect hash missing",
            )
        require((output / "driver.py").read_text() == DRIVER, "Retained driver differs")
        lifecycle(output, report, rows)
        results = output / "results"
        probe = document(results / "executor-probe.json")
        validate_probe(probe, packages)
        result = document(results / "validation.json")
        require(
            not any(key in result for key in ("error", "error_type")),
            "Validation reports an error",
        )
        expected_argv = [
            "/opt/ci/bin/python",
            "-m",
            "pytest",
            "tests/",
            "-q",
            "-W",
            "error",
            "-n",
            "2",
            "-o",
            "cache_dir=/tmp/pytest-cache",
            "--cov=app",
            "--cov-branch",
            "--cov-report=json:/evidence/coverage.json",
            "--cov-report=",
            "--junitxml=/evidence/junit.xml",
        ]
        require(
            result["pytest_command"] == expected_argv, "Full pytest command differs"
        )
        require(
            result["passed"] is True
            and type(result["pytest_exit_code"]) is int
            and result["pytest_exit_code"] == 0
            and result["python_executable"] == expected_argv[0]
            and result["probe_packages"] == len(packages),
            "Real pytest/probe execution failed",
        )
        require(report["results"] == result, "Manifest/results differ")
        coverage = retained_results(
            root / "docker/core",
            results / ".coverage",
            document(results / "coverage.json"),
            document(results / "coverage-recomputed.json"),
        )
        require(
            result["coverage"] == coverage
            and all(
                type(result["coverage"][key]) is type(value)
                for key, value in coverage.items()
            ),
            "Validated coverage summary differs",
        )
        from .core_profile import coverage_database

        require(
            result["retained_coverage"]
            == {
                **coverage_database(results / ".coverage"),
                "sha256": sha256(results / ".coverage"),
            },
            "Retained observation identity differs",
        )
        junit = junit_results(results / "junit.xml")
        require(
            result["junit"] == junit
            and all(type(result["junit"][key]) is int for key in junit),
            "Validated JUnit summary differs",
        )
        log = (results / "pytest.log").read_text()
        require(
            not re.search(
                r"\b\w*Warning\b|coroutine .*never awaited|warnings summary|\bdeselected\b",
                log,
                re.I,
            ),
            "Unexpected pytest warning/deselection",
        )
        summaries = re.findall(
            r"(?:^|\n)(\d+) passed(?:, (\d+) skipped)? in [0-9.]+s(?: \([^\n]*\))?(?:\n|$)",
            log,
        )
        require(
            len(summaries) == 1
            and tuple(map(int, (summaries[0][0], summaries[0][1] or "0")))
            == (junit["passed"], junit["skipped"]),
            "Authoritative pytest/JUnit counts differ",
        )
        require(
            inventory(output) == before, "Original evidence changed during validation"
        )
        bind_source(root, output, revision, report)
        return {
            "source_revision": revision,
            "certifying": True,
            "coverage": coverage,
            "junit": junit,
        }
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        ResourceFailure,
        SourceFailure,
        InvalidExecutor,
        InvalidResults,
        tarfile.TarError,
    ) as exc:
        if isinstance(exc, InvalidEvidence):
            raise
        raise InvalidEvidence(f"Invalid full-core evidence: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("root", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--max-age-days", type=int, default=30)
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                validate(args.root, args.output, args.revision, args.max_age_days)
            )
        )
        return 0
    except InvalidEvidence as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
