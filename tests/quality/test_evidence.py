"""Evidence must establish the named requirement, not just contain true flags."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/quality"))
from checks import complexity, final_image, mutations  # noqa: E402
from evidence import InvalidEvidence, load_bundle  # noqa: E402

REVISION = "a" * 40


@pytest.mark.parametrize("scores", [[], [float("nan")], [1] * 20 + [21]])
def test_complexity_rejects_empty_nonfinite_and_individual_outlier(
    monkeypatch, tmp_path, scores
):
    raw = {"app/a.py": [{"type": "function", "complexity": score} for score in scores]}
    monkeypatch.setattr("checks.run", lambda *args: json.dumps(raw))
    with pytest.raises(InvalidEvidence):
        complexity(tmp_path, mean_limit=10, max_limit=20)


def test_complexity_accepts_boundary_and_excludes_duplicate_class_aggregate(
    monkeypatch, tmp_path
):
    raw = {
        "app/a.py": [
            {
                "type": "class",
                "complexity": 50,
                "methods": [
                    {"type": "method", "complexity": 10},
                    {"type": "method", "complexity": 10},
                ],
            }
        ]
    }
    monkeypatch.setattr("checks.run", lambda *args: json.dumps(raw))
    assert "2 callables" in complexity(tmp_path, mean_limit=10, max_limit=20)


def bundle(directory: Path) -> dict:
    output = directory / "pytest.log"
    output.write_text("51 passed, 0 warnings; measured coverage96%\n")
    payload = {
        "schema_version": 1,
        "requirement": "TEST-1",
        "source_revision": REVISION,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "commands": [
            {
                "argv": ["python", "-m", "pytest"],
                "exit_code": 0,
                "output": output.name,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            }
        ],
        "assertions": [
            {"name": name, "passed": True}
            for name in (
                "suite_passed",
                "coverage_at_least_95",
                "all_runtime_modules_measured",
                "skip_ceiling_75",
                "no_unexpected_warnings",
            )
        ],
    }
    return payload


def save_bundle(directory: Path, payload: dict) -> None:
    (directory / "TEST-1.json").write_text(json.dumps(payload))


def test_valid_bound_bundle_retains_raw_output(tmp_path):
    payload = bundle(tmp_path)
    save_bundle(tmp_path, payload)
    assert load_bundle(tmp_path, "TEST-1", REVISION, 30) == payload


def test_unrelated_true_assertions_cannot_certify_coverage(tmp_path):
    payload = bundle(tmp_path)
    payload["assertions"] = [{"name": "file_exists", "passed": True}]
    save_bundle(tmp_path, payload)
    with pytest.raises(InvalidEvidence, match="required assertions"):
        load_bundle(tmp_path, "TEST-1", REVISION, 30)


def test_boolean_false_is_not_an_executed_zero_exit_code(tmp_path):
    payload = bundle(tmp_path)
    payload["commands"][0]["exit_code"] = False
    save_bundle(tmp_path, payload)
    with pytest.raises(InvalidEvidence, match="command"):
        load_bundle(tmp_path, "TEST-1", REVISION, 30)


@pytest.mark.parametrize(
    "change", ["revision", "stale", "future", "altered", "failed", "empty"]
)
def test_bundle_rejects_mismatched_or_invalid_evidence(tmp_path, change):
    payload = bundle(tmp_path)
    if change == "revision":
        payload["source_revision"] = "b" * 40
    elif change == "stale":
        payload["observed_at"] = (
            datetime.now(timezone.utc) - timedelta(days=31)
        ).isoformat()
    elif change == "future":
        payload["observed_at"] = (
            datetime.now(timezone.utc) + timedelta(days=1)
        ).isoformat()
    elif change == "altered":
        (tmp_path / "pytest.log").write_text("different output")
    elif change == "failed":
        payload["commands"][0]["exit_code"] = 1
    else:
        payload["commands"] = []
    save_bundle(tmp_path, payload)
    with pytest.raises(InvalidEvidence):
        load_bundle(tmp_path, "TEST-1", REVISION, 30)


def test_output_symlink_cannot_escape_bundle_directory(tmp_path):
    inner = tmp_path / "bundle"
    inner.mkdir()
    payload = bundle(inner)
    (tmp_path / "outside.log").write_text("private unrelated material")
    (inner / "pytest.log").unlink()
    (inner / "pytest.log").symlink_to(tmp_path / "outside.log")
    save_bundle(inner, payload)
    with pytest.raises(InvalidEvidence, match="escapes"):
        load_bundle(inner, "TEST-1", REVISION, 30)


def mutation_report(root: Path) -> dict:
    (root / "docs").mkdir()
    modules = ("app/affection.py", "app/audit_chain.py", "app/personality/speech.py")
    return {
        "source_revision": REVISION,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "complete": True,
        "mutants": [
            {
                "id": f"mutant-{i}",
                "module": modules[i % 3],
                "status": "killed" if i < 8 else "survived",
            }
            for i in range(10)
        ],
    }


def test_real_mutation_score_accepts_exact_threshold(tmp_path):
    data = mutation_report(tmp_path)
    (tmp_path / "docs/mutation-results.json").write_text(json.dumps(data))
    assert "8/10" in mutations(tmp_path, REVISION, 0.8, 30)


@pytest.mark.parametrize(
    "change",
    ["unexecuted", "duplicate", "missing_module", "below_threshold", "incomplete"],
)
def test_mutation_score_rejects_unproved_cases(tmp_path, change):
    data = mutation_report(tmp_path)
    if change == "unexecuted":
        data["mutants"][0]["status"] = "timeout"
    elif change == "duplicate":
        data["mutants"][1]["id"] = data["mutants"][0]["id"]
    elif change == "missing_module":
        for mutant in data["mutants"]:
            mutant["module"] = "app/affection.py"
    elif change == "below_threshold":
        data["mutants"][0]["status"] = "survived"
    else:
        data["complete"] = False
    (tmp_path / "docs/mutation-results.json").write_text(json.dumps(data))
    with pytest.raises(InvalidEvidence):
        mutations(tmp_path, REVISION, 0.8, 30)


def test_pinned_distroless_nonroot_stage_is_parsed(tmp_path):
    core = tmp_path / "docker/core"
    core.mkdir(parents=True)
    (core / "Dockerfile").write_text(
        "ARG RUNTIME=gcr.io/distroless/base-debian13:nonroot@sha256:"
        + "a" * 64
        + "\nFROM python:3.14-slim AS build\nFROM ${RUNTIME}\nUSER 1000:1000\n"
    )
    assert "final FROM" in final_image(tmp_path)
