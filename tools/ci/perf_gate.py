"""Fail-closed paired-image performance gate; never refreshes baseline evidence."""

from __future__ import annotations
import argparse
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import re
from typing import Any

from .perf_schema import (
    MIN_ROUNDS,
    MIN_SAMPLES,
    TARGETS,
    digest,
    integer,
    number,
    object_dict,
    object_list,
)


RATIO_BUDGET = 1.2
# p99 of 100 samples is the second-slowest request, so two scheduler hiccups
# push a 2 ms route past 20%. A regression must also cost more than this
# (owner decision 2026-10-16, after five runs flagged a different ms route each).
NOISE_FLOOR_MS = 5.0
# One noisy round is not a regression: a route fails only when it regresses in
# at least this many measured rounds (owner decision 2026-10-17, after an A/A
# nightly failed on a single /api/tts round with a pooled ratio of 1.12).
REGRESSED_ROUNDS_REQUIRED = 2


def regressed(reference: float, measured: float) -> bool:
    return (
        measured / reference > RATIO_BUDGET + 1e-12
        and measured - reference > NOISE_FLOOR_MS
    )


def p99(values: list[float]) -> float:
    if not values:
        raise ValueError("Missing percentile samples")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.99 * len(ordered)) - 1)]


def image_reference(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[A-Za-z0-9._:/-]+@)?sha256:[a-f0-9]{64}", value
    ):
        raise ValueError("An explicit full immutable image ID/digest is required")
    return value


def revision(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value):
        raise ValueError("An explicit full source revision is required")
    return value


def _timestamps(report: dict[str, Any]) -> None:
    try:
        start, end = (
            datetime.fromisoformat(report[key]) for key in ("started_at", "finished_at")
        )
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError("Missing timestamp evidence") from error
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise ValueError("Invalid timestamp ordering/timezone")
    age = (datetime.now(UTC) - end).total_seconds()
    if not -30 <= age <= 86400:
        raise ValueError("Stale or future performance evidence")


def _identity(
    report: dict[str, Any],
    role: str,
    expected_image: str,
    expected_revision: str,
    allow_development: bool,
) -> None:
    image_reference(expected_image)
    revision(expected_revision)
    if (
        type(report.get("schema")) is not int
        or report["schema"] != 1
        or report.get("role") != role
        or not report.get("run_id")
    ):
        raise ValueError("Mismatched report identity")
    image = object_dict(report.get("image"))
    image_reference(image.get("id"))
    if (
        image.get("input", image.get("id")) != expected_image
        or image.get("declared_revision") != expected_revision
    ):
        raise ValueError("Image/source input mismatch")
    if expected_image.startswith("sha256:") and image["id"] != expected_image:
        raise ValueError("Resolved Docker image ID does not match the input")
    if not allow_development and report.get("development_only") is not False:
        raise ValueError("Development evidence cannot certify CI")
    verified = (
        image.get("verified") is True
        and image.get("image_revision") == expected_revision
    )
    if not verified and not (
        allow_development and report.get("development_only") is True
    ):
        raise ValueError("Unverified or mismatched image revision")
    if report.get("cleanup_verified") is not True:
        raise ValueError("Disposable resources were not verified clean")
    telemetry = report.get("thermal_telemetry", "sensors")
    if telemetry == "virtualized-guest":
        if report.get("cpu_temperature_max_c") is not None:
            raise ValueError("Virtualized evidence cannot carry a sensor reading")
    elif telemetry != "sensors":
        raise ValueError("Unknown thermal telemetry")
    elif number(report.get("cpu_temperature_max_c")) >= 70:
        raise ValueError("Thermal safety budget exceeded")


def _configuration(report: dict[str, Any]) -> dict[str, Any]:
    config = object_dict(report.get("configuration"))
    if (
        set(config) != {"samples", "rounds", "warmup", "concurrency", "seed_version"}
        or integer(config.get("seed_version")) != 1
    ):
        raise ValueError("Missing/noncanonical deterministic seed configuration")
    integer(config.get("samples"), MIN_SAMPLES)
    integer(config.get("rounds"), MIN_ROUNDS)
    integer(config.get("warmup"))
    if integer(config.get("concurrency")) != 1:
        raise ValueError("Comparable performance requires concurrency1")
    resources = object_dict(report.get("resources"))
    if set(resources) != {"core", "postgres", "redis", "qdrant", "fixture"}:
        raise ValueError("Incomplete resource evidence")
    total_cpus = total_ram = 0
    for resource in resources.values():
        resource = object_dict(resource)
        total_cpus += integer(resource.get("nano_cpus"))
        total_ram += integer(resource.get("memory_bytes"))
    if total_cpus > 3_000_000_000 or total_ram > 2 * 1024**3:
        raise ValueError("Disposable resource budget exceeded")
    return config


def _route(
    row: dict[str, Any], config: dict[str, Any]
) -> tuple[tuple[str, str], list[float], float]:
    identity = (str(row.get("method")), str(row.get("route")))
    expected = {(method, route): status for method, route, status in TARGETS}
    if (
        identity not in expected
        or type(row.get("warmup_count")) is not int
        or row["warmup_count"] != config["warmup"]
    ):
        raise ValueError("Unknown route or missing warmup evidence")
    samples = object_list(row.get("samples"))
    if len(samples) != config["samples"]:
        raise ValueError("Missing measured samples")
    successful = []
    for sample in samples:
        sample = object_dict(sample)
        elapsed = number(sample.get("elapsed_ms"))
        status = integer(sample.get("status"), 100)
        if status > 599 or type(sample.get("contract_ok")) is not bool:
            raise ValueError("Invalid public contract evidence")
        if sample["contract_ok"] and status != expected[identity]:
            raise ValueError("401/404 or wrong status cannot be a healthy sample")
        if sample["contract_ok"] and status == expected[identity]:
            successful.append(elapsed)
    if not successful:
        raise ValueError("No successful authenticated/contract samples")
    return (
        (str(identity[0]), str(identity[1])),
        successful,
        len(successful) / len(samples),
    )


def validate_report(
    report: dict[str, Any],
    *,
    role: str,
    policy: dict[str, Any],
    expected_image: str,
    expected_revision: str,
    allow_development: bool = False,
) -> list[dict[tuple[str, str], tuple[list[float], float]]]:
    _identity(report, role, expected_image, expected_revision, allow_development)
    _timestamps(report)
    config = _configuration(report)
    provenance = object_dict(report.get("provenance"))
    for field in ("harness", "fixture", "seed", "policy", "host"):
        if not isinstance(provenance.get(field), str) or not re.fullmatch(
            r"[a-f0-9]{64}", provenance[field]
        ):
            raise ValueError("Missing source/configuration fingerprint")
    if provenance["policy"] != digest(policy):
        raise ValueError("Canonical SLO policy mismatch")
    rounds = object_list(report.get("rounds"))
    if len(rounds) != config["rounds"]:
        raise ValueError("Missing comparable rounds")
    result = []
    for index, run in enumerate(rounds):
        run = object_dict(run)
        if type(run.get("round")) is not int or run["round"] != index:
            raise ValueError("Duplicate/mismatched round identity")
        actual = object_dict(run.get("actual_container"))
        if (
            actual.get("image") != report["image"]["id"]
            or actual.get("resources") != report["resources"]["core"]
        ):
            raise ValueError("Actual measured container image/resources mismatch")
        routes = {}
        for row in object_list(run.get("routes")):
            identity, values, availability = _route(object_dict(row), config)
            if identity in routes:
                raise ValueError("Duplicate route evidence")
            routes[identity] = (values, availability)
        if set(routes) != {(method, route) for method, route, _ in TARGETS}:
            raise ValueError("Incomplete public route coverage")
        result.append(routes)
    return result


def _assess(
    baseline: dict[tuple[str, str], tuple[list[float], float]],
    candidate: dict[tuple[str, str], tuple[list[float], float]],
    slos: dict[tuple[str, str], dict[str, Any]],
    label: str,
) -> list[dict[str, Any]]:
    result = []
    for identity, (values, availability) in candidate.items():
        reference = p99(baseline[identity][0])
        measured = p99(values)
        ratio = measured / reference
        failures = []
        objective = slos.get(identity)
        minimum = number(objective["availability"]) if objective else 1.0
        if availability < minimum or baseline[identity][1] < minimum:
            failures.append(f"{label} {identity}: availability objective failed")
        if objective and measured > number(objective["p99_ms"]):
            failures.append(f"{label} {identity}: canonical absolute p99 SLO failed")
        result.append(
            {
                "round": label,
                "method": identity[0],
                "route": identity[1],
                "baseline_p99_ms": reference,
                "candidate_p99_ms": measured,
                "p99_ratio": ratio,
                "regressed": regressed(reference, measured),
                "availability": availability,
                "absolute_slo": objective,
                "failures": failures,
            }
        )
    return result


def compare(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    policy: dict[str, Any],
    baseline_image: str,
    candidate_image: str,
    baseline_revision: str,
    candidate_revision: str,
    allow_development: bool = False,
) -> dict[str, Any]:
    inputs = []
    for report, role, image, source in (
        (baseline, "baseline", baseline_image, baseline_revision),
        (candidate, "candidate", candidate_image, candidate_revision),
    ):
        inputs.append(
            validate_report(
                report,
                role=role,
                policy=policy,
                expected_image=image,
                expected_revision=source,
                allow_development=allow_development,
            )
        )
    for field in ("run_id", "configuration", "resources", "provenance"):
        if baseline[field] != candidate[field]:
            raise ValueError(f"Noncomparable {field} evidence")
    slos = {(row["method"], row["route"]): row for row in policy["endpoints"]}
    results = [
        row
        for index, (reference, measured) in enumerate(zip(*inputs, strict=True))
        for row in _assess(reference, measured, slos, str(index))
    ]
    pooled = []
    for rounds in inputs:
        pooled.append(
            {
                identity: (
                    [value for run in rounds for value in run[identity][0]],
                    sum(run[identity][1] for run in rounds) / len(rounds),
                )
                for identity in rounds[0]
            }
        )
    results.extend(_assess(pooled[0], pooled[1], slos, "pooled"))
    failures = [failure for row in results for failure in row["failures"]]
    measured_rounds = [row for row in results if row["round"] != "pooled"]
    for identity in dict.fromkeys((r["method"], r["route"]) for r in measured_rounds):
        hits = [
            r["round"]
            for r in measured_rounds
            if (r["method"], r["route"]) == identity and r["regressed"]
        ]
        if len(hits) >= REGRESSED_ROUNDS_REQUIRED:
            failures.append(
                f"{identity}: p99 regression exceeds20percent and5ms in "
                f"{len(hits)} of {len(inputs[0])} rounds ({','.join(hits)})"
            )
    return {
        "schema": 1,
        "passed": not failures,
        "certifying": not allow_development,
        "max_p99_ratio": max(row["p99_ratio"] for row in results),
        "noise_floor_ms": NOISE_FLOOR_MS,
        "regressed_rounds_required": REGRESSED_ROUNDS_REQUIRED,
        "failures": failures,
        "comparisons": results,
        "timestamp": datetime.now(UTC).isoformat(),
        "run_id": baseline["run_id"],
        "baseline_image": baseline_image,
        "candidate_image": candidate_image,
        "baseline_revision": baseline_revision,
        "candidate_revision": candidate_revision,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "candidate", "policy", "out"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    for name in (
        "baseline-image",
        "candidate-image",
        "baseline-revision",
        "candidate-revision",
    ):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args()
    try:
        result = compare(
            json.loads(args.baseline.read_text()),
            json.loads(args.candidate.read_text()),
            policy=json.loads(args.policy.read_text()),
            baseline_image=args.baseline_image,
            candidate_image=args.candidate_image,
            baseline_revision=args.baseline_revision,
            candidate_revision=args.candidate_revision,
        )
        with args.out.open("x") as output:
            json.dump(result, output, indent=2, allow_nan=False)
            output.write("\n")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(
            1, f"Performance gate failed closed: {type(error).__name__}: {error}\n"
        )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
