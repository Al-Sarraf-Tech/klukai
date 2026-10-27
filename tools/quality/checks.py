"""Executable source checks and revision-bound measurement validation."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from evidence import InvalidEvidence, load_bundle
from runtime_scope import python_sources, runtime_sources


def run(argv: list[str], cwd: Path, timeout: int = 300) -> str:
    if shutil.which(argv[0]) is None:
        raise InvalidEvidence(f"required tool missing: {argv[0]}")
    result = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        raise InvalidEvidence(f"{argv[0]} failed with exit {result.returncode}")
    return result.stdout


def python_checks(root: Path) -> str:
    core = root / "docker/core"
    run(["ruff", "check", "app/", "--config", "ruff.toml"], core)
    run(["ruff", "format", "--check", "app/"], core)
    run(
        ["mypy", "app/", "--strict", "--strict-optional", "--config-file", "mypy.ini"],
        core,
    )
    return "blocking lint, formatter and strict types passed"


def structure(root: Path, limit: int) -> str:
    offenders = []
    for path in runtime_sources(root):
        count = len(path.read_text().splitlines())
        if count >= limit:
            offenders.append(f"{path.relative_to(root)}:{count}")
    if offenders:
        raise InvalidEvidence(
            "oversized application modules: " + ", ".join(sorted(offenders))
        )
    contract = root / "tests/quality/test_architecture.py"
    if not contract.exists():
        raise InvalidEvidence("architecture contract has not been implemented")
    run(["python3", "-m", "pytest", str(contract), "-q"], root)
    return "application size and executable architecture contract passed"


def complexity(root: Path, mean_limit: float, max_limit: int) -> str:
    sources = [path.relative_to(root).as_posix() for path in python_sources(root)]
    if not sources:
        raise InvalidEvidence("no maintained Python runtime to measure")
    payload = json.loads(run(["radon", "cc", *sources, "-j"], root))
    scores: list[float] = []

    def visit(block: dict[str, Any]) -> None:
        score = block.get("complexity")
        if (
            not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not math.isfinite(score)
            or score < 1
        ):
            raise InvalidEvidence("invalid numeric complexity measurement")
        if block.get("type") != "class":
            scores.append(float(score))
        for child in block.get("methods", []) + block.get("closures", []):
            visit(child)

    if not isinstance(payload, dict):
        raise InvalidEvidence("invalid complexity report")
    for blocks in payload.values():
        for block in blocks:
            visit(block)
    if not scores:
        raise InvalidEvidence("complexity report contains no callables")
    mean, maximum = sum(scores) / len(scores), max(scores)
    if mean > mean_limit or maximum > max_limit:
        raise InvalidEvidence(
            f"complexity mean={mean:.3f} max={maximum:g}; bounds={mean_limit:g}/{max_limit}"
        )
    return f"{len(scores)} callables: mean={mean:.3f}, max={maximum:g}"


def final_image(root: Path) -> str:
    arguments: dict[str, str] = {}
    stages: list[str] = []
    user = "root"
    for line in (root / "docker/core/Dockerfile").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        directive, _, body = line.partition(" ")
        if directive.upper() == "ARG" and "=" in body:
            key, value = body.split("=", 1)
            arguments[key] = value
        elif directive.upper() == "FROM":
            image = body.split()[0]
            for key, value in arguments.items():
                image = image.replace("${" + key + "}", value).replace("$" + key, value)
            stages.append(image)
            user = "root"
        elif directive.upper() == "USER":
            user = body.split()[0]
    if not stages or not re.fullmatch(
        r"(?:gcr\.io/distroless/|cgr\.dev/chainguard/)[^\s@]+@sha256:[a-f0-9]{64}",
        stages[-1],
    ):
        raise InvalidEvidence("final FROM is not a digest-pinned distroless base")
    if user.split(":")[0] in ("root", "0"):
        raise InvalidEvidence("runtime USER is root")
    return "actual final FROM and USER passed; artifact scan still required separately"


def mutations(
    root: Path,
    revision: str,
    threshold: float,
    max_age: int,
    evidence: Path | None = None,
) -> str:
    directory = evidence if evidence is not None else root / ".audit-evidence/TEST-3"
    if not (directory / "mutation.json").is_file():
        raise InvalidEvidence(
            "missing executed mutation bundle; summary rows are not proof"
        )
    if not 0.8 <= threshold <= 1 or not 1 <= max_age <= 30:
        raise InvalidEvidence(
            "mutation policy cannot weaken the execution requirements"
        )
    # This validator checks the actual tool, source/tests, complete generated
    # inventory, per-mutant pytest phase/exit records, timestamps and raw hashes.
    raw = run(
        [
            sys.executable,
            "-m",
            "tools.ci.mutation",
            "validate",
            "--root",
            str(root),
            "--output",
            str(directory),
            "--revision",
            revision,
            "--threshold",
            str(threshold),
            "--max-age-days",
            str(max_age),
        ],
        root,
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidEvidence("invalid mutation validator output") from exc
    if not isinstance(data, dict) or data.get("development_only") is not False:
        raise InvalidEvidence("mutation validator did not establish final execution")
    killed, total, rate = data.get("killed"), data.get("total"), data.get("kill_rate")
    if (
        type(killed) is not int
        or type(total) is not int
        or not 0 <= killed <= total
        or total < 1
        or type(rate) not in (int, float)
        or not math.isfinite(rate)
        or rate != killed / total
        or rate < threshold
    ):
        raise InvalidEvidence("invalid validated mutation score")
    return f"{killed}/{total} execution-verified mutants killed ({rate:.2%})"


def core_tests(root: Path, revision: str, max_age: int, evidence: Path) -> str:
    """Consume actual managed output through the requirement-specific CLI."""
    raw = run(
        [
            sys.executable,
            "-B",
            "-m",
            "tools.ci.core_evidence",
            "--root",
            str(root),
            "--output",
            str(evidence),
            "--revision",
            revision,
            "--max-age-days",
            str(max_age),
        ],
        root,
    )
    try:
        result = json.loads(raw)
        coverage, junit = result["coverage"], result["junit"]
        if result["certifying"] is not True or result["source_revision"] != revision:
            raise InvalidEvidence(
                "core semantic verifier did not establish final execution"
            )
        for key in ("statements_percent", "branches_percent"):
            value = coverage[key]
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 95 <= value <= 100
            ):
                raise InvalidEvidence("invalid independently measured core coverage")
        if any(
            type(junit[key]) is not int for key in ("passed", "skipped", "tests")
        ) or not (
            junit["passed"] > 0
            and 0 <= junit["skipped"] <= 75
            and junit["tests"] == junit["passed"] + junit["skipped"]
        ):
            raise InvalidEvidence("invalid independently measured JUnit results")
        return (
            f"SQLite/source-verified statements={coverage['statements_percent']:.2f}% "
            f"branches={coverage['branches_percent']:.2f}%; "
            f"{junit['passed']} passed, {junit['skipped']} skipped"
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise InvalidEvidence("invalid core semantic verifier result") from exc


def evaluate(
    check: dict[str, Any],
    root: Path,
    revision: str,
    policy: dict[str, Any],
    evidence: Path,
) -> str:
    limits = policy["limits"]
    match check["kind"]:
        case "python":
            return python_checks(root)
        case "structure":
            return structure(root, limits["module_lines"])
        case "complexity":
            return complexity(root, limits["complexity_mean"], limits["complexity_max"])
        case "image":
            return final_image(root)
        case "mutation":
            return mutations(
                root,
                revision,
                limits["mutation_kill_rate"],
                limits["evidence_max_age_days"],
                evidence / "TEST-3",
            )
        case "secrets":
            run(["gitleaks", "git", "--no-banner", "--redact", "--log-opts=HEAD"], root)
            return "required secret scan passed"
        case "evidence":
            if check["id"] == "PERF-1":
                return performance(
                    root, revision, limits["evidence_max_age_days"], evidence / "PERF-1"
                )
            if (
                check["id"] == "TEST-1"
                and (evidence / "TEST-1/manifest.json").is_file()
            ):
                return core_tests(
                    root, revision, limits["evidence_max_age_days"], evidence / "TEST-1"
                )
            load_bundle(
                evidence, check["id"], revision, limits["evidence_max_age_days"]
            )
            # Integrity is necessary but does not interpret a measurement or
            # prove that the named assertions follow from command output.
            raise InvalidEvidence(
                f"{check['id']} needs a requirement-specific semantic verifier; "
                "named true assertions and output hashes cannot certify it"
            )
        case _:
            raise InvalidEvidence("unknown check kind")


# Eight measured routes (tools/ci/perf_schema.TARGETS) in at least three paired
# rounds plus the pooled comparison. Voice left the gate on 2026-10-26.
MIN_PERF_COMPARISONS = 8 * (3 + 1)


def performance(root: Path, revision: str, max_age: int, evidence: Path) -> str:
    """Actual producer output is mandatory; generic named assertions never suffice."""
    raw = run(
        [
            sys.executable,
            "-B",
            "-m",
            "tools.ci.perf_evidence",
            "--root",
            str(root),
            "--output",
            str(evidence),
            "--revision",
            revision,
            "--max-age-days",
            str(max_age),
        ],
        root,
    )
    try:
        result = json.loads(raw)
        ratio = result["max_p99_ratio"]
        if (
            result["certifying"] is not True
            or result["source_revision"] != revision
            or type(ratio) not in (int, float)
            or not math.isfinite(ratio)
            or not 0 < ratio
            or result.get("noise_floor_ms") != 5.0
            or type(result["comparisons"]) is not int
            or result["comparisons"] < MIN_PERF_COMPARISONS
        ):
            raise InvalidEvidence("invalid independently verified performance result")
        return f"Source/lifecycle-verified performance: max p99 ratio={ratio:.4f}; {result['comparisons']} comparisons"
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidEvidence("invalid performance semantic verifier result") from exc
