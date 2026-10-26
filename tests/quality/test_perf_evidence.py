"""Actual-format performance evidence must be independently consumable."""

from pathlib import Path
from datetime import UTC, datetime, timedelta
import json
import subprocess
import sys

import pytest


def test_strict_raw_parser_rejects_ambiguous_or_nonfinite_json():
    from tools.ci.perf_evidence_identity import loads

    for raw in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'):
        with pytest.raises(ValueError):
            loads(raw)


@pytest.mark.parametrize(
    "millis,passed",
    # Baseline 10 ms: a regression needs >20% AND >5 ms (noise floor).
    [(11, True), (12, True), (12.01, True), (15, True), (15.01, False)],
)
def test_real_format_bundle_recalculates_latency(tmp_path, millis, passed):
    from perf_evidence_fixture import bundle
    from tools.ci.perf_evidence import validate

    root, output, revision = bundle(tmp_path, millis)
    if passed:
        result = validate(root, output, revision, 1)
        assert result["certifying"] is True
        assert result["max_p99_ratio"] == pytest.approx(millis / 10)
    else:
        with pytest.raises(ValueError, match="performance gate"):
            validate(root, output, revision, 1)


def test_assertions_only_bundle_fails_with_semantic_diagnostic(tmp_path):
    (tmp_path / "manifest.json").write_text('{"passed":true}')
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "tools.ci.perf_evidence",
            "--root",
            str(Path.cwd()),
            "--output",
            str(tmp_path),
            "--revision",
            "a" * 40,
            "--max-age-days",
            "1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Performance evidence rejected:" in result.stderr


def rewrite(output, filename, mutate):
    path = output / filename
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data, indent=2) + "\n")


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "99samples",
        "2rounds",
        "unknownroute",
        "boolsample",
        "infinity",
        "dupekey",
        "warmup",
        "concurrency",
        "boolwarmup",
        "boolround",
        "boolschema",
        "sameimage",
        "samesource",
        "wrongcandidate",
        "wrongbaseline",
        "stale",
        "future",
        "development",
        "source",
        "archive",
        "harness",
        "policy",
        "seed",
        "host",
        "rawround",
        "rehashedround",
        "absolute",
        "badround",
        "external",
        "gpu",
        "boolfixture",
        "fixturecounts",
        "fixtureidle",
        "fixturereset",
        "unknownfile",
        "symlink",
        "hardlink",
        "emptydir",
    ],
)
def test_rehashed_inconsistent_artifacts_never_certify(tmp_path, mutation):
    from perf_evidence_fixture import bundle, rehash
    from tools.ci.perf_evidence import validate

    root, output, revision = bundle(tmp_path)
    if mutation in {"source", "archive"}:
        path = output / (
            "source/config/personality.yaml" if mutation == "source" else "source.tar"
        )
        path.write_bytes(path.read_bytes() + b"changed")
    elif mutation == "harness":
        rewrite(
            output,
            "harness-sources.json",
            lambda data: data["files"].update({"perf_seed.py": "changed"}),
        )
    elif mutation == "policy":
        rewrite(
            output,
            "inputs.json",
            lambda data: data["policy"]["endpoints"][0].update(p99_ms=100000),
        )
    elif mutation == "host":
        rewrite(
            output,
            "inputs.json",
            lambda data: data.update(host={"platform": "fabricated"}),
        )
    elif mutation == "unknownfile":
        (output / "unrelated.json").write_text("{}")
    elif mutation == "emptydir":
        (output / "unknown").mkdir()
    elif mutation == "symlink":
        (output / "link").symlink_to(output / "baseline.json")
    elif mutation == "hardlink":
        import os

        os.link(output / "baseline.json", output / "linked.json")
    elif mutation in {"rawround", "rehashedround"}:
        rewrite(
            output,
            "candidate-round-0.json",
            lambda data: data["routes"][0]["samples"].pop(),
        )
    elif mutation == "dupekey":
        path = output / "candidate.json"
        path.write_text(
            path.read_text().replace('"schema": 1,', '"schema":1,"schema":1,')
        )
    else:

        def change(data):
            run = data["rounds"][0]
            sample = run["routes"][0]["samples"][0]
            if mutation == "missing":
                run["routes"].pop()
            elif mutation == "99samples":
                run["routes"][0]["samples"].pop()
            elif mutation == "2rounds":
                data["configuration"]["rounds"] = 2
                data["rounds"].pop()
            elif mutation == "unknownroute":
                run["routes"][0]["route"] = "/unmeasured"
            elif mutation in {"boolsample", "infinity"}:
                sample["elapsed_ms"] = (
                    True if mutation == "boolsample" else float("inf")
                )
            elif mutation in {"warmup", "concurrency"}:
                data["configuration"][mutation] = 0 if mutation == "warmup" else 2
            elif mutation == "boolwarmup":
                data["configuration"]["warmup"] = 1
                for row in data["rounds"]:
                    for route in row["routes"]:
                        route["warmup_count"] = True
            elif mutation == "boolround":
                run["round"] = False
            elif mutation == "boolschema":
                data["schema"] = True
            elif mutation in {
                "sameimage",
                "samesource",
                "wrongcandidate",
                "wrongbaseline",
            }:
                image = data["image"]
                image["id" if mutation == "sameimage" else "declared_revision"] = (
                    "sha256:" + "a" * 64
                    if mutation == "sameimage"
                    else revision
                    if mutation == "samesource"
                    else "f" * 40
                )
            elif mutation in {"stale", "future"}:
                data["finished_at"] = (
                    datetime.now(UTC) + timedelta(days=-2 if mutation == "stale" else 1)
                ).isoformat()
            elif mutation == "development":
                data["development_only"] = True
            elif mutation == "seed":
                data["provenance"]["seed"] = "f" * 64
            elif mutation in {"absolute", "badround"}:
                for value in run["routes"][0]["samples"]:
                    value["elapsed_ms"] = 31 if mutation == "absolute" else 20
            elif mutation in {"external", "gpu", "boolfixture"}:
                run["fixture"][
                    "external_calls" if mutation != "gpu" else "gpu_calls"
                ] = False if mutation == "boolfixture" else 1
            elif mutation == "fixturecounts":
                run["fixture"]["counts"] = {}
            elif mutation == "fixtureidle":  # this core never reached the fixture
                run["fixture"]["counts"]["POST /mcp"] -= 1
            elif mutation == "fixturereset":  # the next round's counters shrink
                run["fixture"]["counts"]["GET /v1/models"] = 5
            # Keep retained round objects consistent to force semantic interpretation.
            role = (
                "baseline"
                if mutation in {"wrongbaseline", "samesource"}
                else "candidate"
            )
            for index, row in enumerate(data["rounds"]):
                (output / f"{role}-round-{index}.json").write_text(
                    json.dumps(row, indent=2) + "\n"
                )

        rewrite(
            output,
            "baseline.json"
            if mutation in {"wrongbaseline", "samesource"}
            else "candidate.json",
            change,
        )
    if mutation != "symlink":
        rehash(output)
    with pytest.raises((ValueError, KeyError, IndexError, subprocess.SubprocessError)):
        validate(root, output, revision, 30)


def _fixture(counts, **calls):
    return {
        "fixture": "klukai-deterministic-v1",
        "external_calls": 0,
        "gpu_calls": 0,
        "counts": counts,
    } | calls


def test_each_measured_core_must_open_its_own_fixture_session():
    from tools.ci.perf_evidence import fixture_bound

    earlier = {"GET /fixture-stats": 1, "POST /mcp": 1, "GET /v1/models": 1}
    # Round 0 candidate: second core; the cloud-only core calls no LM Studio.
    counts = {"GET /fixture-stats": 2, "POST /mcp": 2, "GET /v1/models": 1}
    assert fixture_bound(_fixture(counts), 0, "candidate", earlier) == counts
    for changed, message in (
        ({"POST /mcp": 1}, "fixture work"),  # no new handshake for this core
        ({"GET /fixture-stats": 3}, "fixture work"),  # wrong round position
        ({"GET /v1/models": 0}, "counters"),  # zero counters are never retained
    ):
        with pytest.raises(ValueError, match=message):
            fixture_bound(_fixture(counts | changed), 0, "candidate", earlier)
    shrunk = {k: v for k, v in counts.items() if k != "GET /v1/models"}
    with pytest.raises(ValueError, match="backwards"):
        fixture_bound(_fixture(shrunk), 0, "candidate", earlier)
    for calls in ({"external_calls": 1}, {"gpu_calls": 1}):
        with pytest.raises(ValueError, match="external/GPU"):
            fixture_bound(_fixture(counts, **calls), 0, "candidate", earlier)


def test_cli_and_audit_consume_real_format_evidence(tmp_path, monkeypatch):
    from perf_evidence_fixture import bundle
    from tools.ci.perf_evidence import main

    root, output, revision = bundle(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "perf_evidence",
            "--root",
            str(root),
            "--output",
            str(output),
            "--revision",
            revision,
            "--max-age-days",
            "1",
        ],
    )
    assert main() == 0
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[2] / "tools/quality")
    )
    from checks import MIN_PERF_COMPARISONS, performance
    from tools.ci.perf_schema import MIN_ROUNDS, TARGETS

    # The audit's floor is exactly the producer's minimum: every route, every round.
    assert MIN_PERF_COMPARISONS == len(TARGETS) * (MIN_ROUNDS + 1)
    assert "max p99 ratio=1.1000" in performance(root, revision, 1, output)


@pytest.mark.parametrize("field", ["schema", "availability"])
def test_rehashed_computed_gate_does_not_alias_booleans_to_numbers(tmp_path, field):
    from perf_evidence_fixture import bundle, rehash
    from tools.ci.perf_evidence import validate

    root, output, revision = bundle(tmp_path)

    def change(gate):
        if field == "schema":
            gate["schema"] = True
        else:
            gate["comparisons"][0]["availability"] = True

    rewrite(output, "gate.json", change)
    rehash(output)
    with pytest.raises(ValueError):
        validate(root, output, revision, 1)
