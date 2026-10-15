"""Executable source checks and revision-bound measurement validation."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evidence import InvalidEvidence, load_bundle, timestamp


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
    for directory, suffix in (("docker/core/app", ".py"), ("flutter_app/lib", ".dart")):
        for path in (root / directory).rglob(f"*{suffix}"):
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
    payload = json.loads(run(["radon", "cc", "docker/core/app", "-j"], root))
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


def mutations(root: Path, revision: str, threshold: float, max_age: int) -> str:
    data = json.loads((root / "docs/mutation-results.json").read_text())
    if data.get("source_revision") != revision:
        raise InvalidEvidence("mutation report does not bind this source revision")
    timestamp(
        data.get("observed_at"), now=datetime.now(timezone.utc), max_age_days=max_age
    )
    mutants = data.get("mutants")
    if data.get("complete") is not True or not isinstance(mutants, list) or not mutants:
        raise InvalidEvidence("mutation execution incomplete or empty")
    required = {"app/affection.py", "app/audit_chain.py", "app/personality/speech.py"}
    modules, ids = set(), set()
    killed = 0
    for item in mutants:
        if not isinstance(item, dict) or item.get("status") not in (
            "killed",
            "survived",
        ):
            raise InvalidEvidence(
                "mutation report includes unresolved/unexecuted cases"
            )
        identity, module = item.get("id"), item.get("module")
        if (
            not isinstance(identity, str)
            or not identity
            or identity in ids
            or not isinstance(module, str)
        ):
            raise InvalidEvidence("invalid or duplicate mutant identity")
        ids.add(identity)
        modules.add(module)
        killed += item["status"] == "killed"
    if not required.issubset(modules):
        raise InvalidEvidence("mutation report omits critical modules")
    rate = killed / len(mutants)
    if rate < threshold:
        raise InvalidEvidence(f"mutation kill rate {rate:.2%} below {threshold:.0%}")
    return f"{killed}/{len(mutants)} actual mutants killed ({rate:.2%})"


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
            )
        case "secrets":
            run(["gitleaks", "git", "--no-banner", "--redact", "--log-opts=HEAD"], root)
            return "required secret scan passed"
        case "evidence":
            payload = load_bundle(
                evidence, check["id"], revision, limits["evidence_max_age_days"]
            )
            return f"{len(payload['assertions'])} assertions; {len(payload['commands'])} intact command outputs"
        case _:
            raise InvalidEvidence("unknown check kind")
