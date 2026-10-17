"""Independently validate retained actual disposable PERF-1 producer evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from . import core_source as source
from .perf_evidence_identity import (
    bind_source,
    commands,
    document,
    fingerprints,
    manifest,
    require,
    timestamps,
)
from .perf_evidence_lifecycle import lifecycle
from .perf_gate import compare, image_reference, revision as full_revision
from .perf_resources import BACKEND_IMAGES, RESOURCES
from .perf_schema import digest


def inputs_bound(output: Path) -> dict[str, Any]:
    inputs = document(output / "inputs.json")
    require(
        set(inputs)
        == {
            "python",
            "httpx",
            "docker",
            "backends",
            "backend_images",
            "backend_inputs",
            "resources",
            "policy",
            "personality_sha256",
            "host",
        }
        and set(inputs["backends"]) == set(BACKEND_IMAGES),
        "Incomplete/unknown producer inputs",
    )
    require(
        set(inputs["backend_inputs"]) == set(BACKEND_IMAGES),
        "Incomplete requested backend identities",
    )
    for reference in inputs["backend_inputs"].values():
        image_reference(reference)
    require(
        type(inputs["python"]) is str
        and re.fullmatch(r"3\.(12|13|14)\.[0-9]+", inputs["python"])
        and type(inputs["httpx"]) is str
        and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", inputs["httpx"]),
        "Invalid producer tool versions",
    )
    require(
        inputs["resources"] == RESOURCES and inputs["backend_images"] == BACKEND_IMAGES,
        "Pinned backend/resource profile differs",
    )
    return inputs


def reports_bound(
    root: Path, output: Path, revision: str, report: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    baseline, candidate = (
        document(output / (role + ".json")) for role in ("baseline", "candidate")
    )
    require(
        all(
            type(r.get("run_id")) is str and re.fullmatch(r"[a-f0-9]{32}", r["run_id"])
            for r in (baseline, candidate)
        ),
        "Invalid producer run identity",
    )
    require(
        candidate["image"]["declared_revision"] == revision, "Wrong candidate revision"
    )
    prior = full_revision(baseline["image"]["declared_revision"])
    require(
        prior != revision and baseline["image"]["id"] != candidate["image"]["id"],
        "Same source/image baseline",
    )
    source.git(root, "cat-file", "-e", prior + "^{commit}")
    require(
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", prior, revision],
            capture_output=True,
            check=False,
        ).returncode
        == 0,
        "Baseline is not a previous source revision",
    )
    inputs = inputs_bound(output)
    policy = document(root / "docker/core/app/observability/slos.json")
    require(
        inputs["policy"] == policy
        and inputs["personality_sha256"]
        == digest((root / "config/personality.yaml").read_text()),
        "Current policy/personality input differs",
    )
    harness = document(output / "harness-sources.json")
    sources = {
        p.name: p.read_text() for p in sorted((root / "tools/ci").glob("perf_*.py"))
    }
    require(
        harness == {"files": sources, "sha256": digest(sources)},
        "Current complete harness differs",
    )
    expected = fingerprints(root, inputs["backend_images"], policy)
    host = inputs["host"]
    require(
        set(host) == {"platform", "machine", "docker", "cpu"}
        and all(type(v) is str and v for v in host.values())
        and host["machine"] == "x86_64"
        and host["platform"].startswith("Linux")
        and "processor" in host["cpu"],
        "Invalid complete host conditions",
    )
    from .perf_evidence_identity import loads

    require(loads(host["docker"]) == inputs["docker"], "Host Docker conditions differ")
    expected["host"] = digest(host)
    for role_report in (baseline, candidate):
        require(
            all(role_report["provenance"].get(k) == v for k, v in expected.items()),
            "Current provenance differs",
        )
    rounds_bound(output, report, baseline, candidate)
    return baseline, candidate, inputs


def rounds_bound(
    output: Path,
    report: dict[str, Any],
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> None:
    outer_start, outer_end = timestamps(report)
    previous = outer_start
    config = candidate["configuration"]
    for role_report in (baseline, candidate):
        require(
            role_report["resources"] == RESOURCES,
            "Actual fixed resource profile differs",
        )
        start, end = timestamps(role_report)
        require(
            outer_start <= start <= end <= outer_end,
            "Report timestamp outside producer",
        )
    for index in range(config["rounds"]):
        for role in (
            ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
        ):
            run = (baseline if role == "baseline" else candidate)["rounds"][index]
            roundfile = output / f"{role}-round-{index}.json"
            require(
                document(roundfile) == run
                and roundfile.read_text()
                == json.dumps(run, indent=2, allow_nan=False) + "\n",
                "Raw round/report bytes/content differ",
            )
            start, end = timestamps(run)
            require(
                previous <= start <= end <= outer_end,
                "Round timestamps/alternation differ",
            )
            previous = end
            fixture_bound(run["fixture"], index, role, config)


def fixture_bound(
    fixture: dict[str, Any], index: int, role: str, config: dict[str, Any]
) -> None:
    require(
        fixture["fixture"] == "klukai-deterministic-v1"
        and all(
            type(fixture[k]) is int and fixture[k] == 0
            for k in ("external_calls", "gpu_calls")
        ),
        "Fixture external/GPU calls",
    )
    counts = fixture["counts"]
    require(
        type(counts) is dict
        and counts
        and all(type(k) is str and type(v) is int and v > 0 for k, v in counts.items()),
        "Invalid fixture counters",
    )
    ordinal = index * 2 + (
        1 if role == ("baseline" if index % 2 == 0 else "candidate") else 2
    )
    require(
        counts.get("GET /fixture-stats") == ordinal
        and counts.get("POST /tts", 0)
        >= ordinal * (config["samples"] + config["warmup"]),
        "Missing actual fixture work",
    )


def validate(root: Path, output: Path, revision: str, max_age: int) -> dict[str, Any]:
    root, output = root.absolute(), output.absolute()
    report = manifest(output, revision, max_age)
    bind_source(root, output, revision, report)
    baseline, candidate, inputs = reports_bound(root, output, revision, report)
    result = compare(
        baseline,
        candidate,
        policy=inputs["policy"],
        baseline_image=image_reference(baseline["image"]["input"]),
        candidate_image=image_reference(candidate["image"]["input"]),
        baseline_revision=baseline["image"]["declared_revision"],
        candidate_revision=revision,
    )
    require(
        result["passed"] and result["certifying"] and result["max_p99_ratio"] <= 1.2,
        "Recomputed performance gate failed",
    )
    retained = document(output / "gate.json")
    from datetime import datetime

    gate_time = datetime.fromisoformat(retained["timestamp"])
    start, end = timestamps(report)
    require(
        gate_time.utcoffset() is not None and start <= gate_time <= end,
        "Stale/future retained gate",
    )
    require(
        set(retained) == set(result)
        and digest({k: v for k, v in retained.items() if k != "timestamp"})
        == digest({k: v for k, v in result.items() if k != "timestamp"}),
        "Retained/recomputed gate differs",
    )
    rows = commands(output)
    prefix = lifecycle(
        output, {"baseline": baseline, "candidate": candidate}, inputs, rows
    )
    from .perf_evidence_identity import raw_inventory

    raw_inventory(output, report, candidate["configuration"]["rounds"], prefix)
    return {
        "certifying": True,
        "source_revision": revision,
        "run_id": result["run_id"],
        "max_p99_ratio": result["max_p99_ratio"],
        "comparisons": len(result["comparisons"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--max-age-days", type=int, default=1)
    args = parser.parse_args()
    try:
        result = validate(args.root, args.output, args.revision, args.max_age_days)
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        IndexError,
        subprocess.SubprocessError,
    ) as error:
        parser.exit(
            1, f"Performance evidence rejected: {type(error).__name__}: {error}\n"
        )
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
