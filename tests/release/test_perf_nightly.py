from __future__ import annotations

import json
from pathlib import Path
import sys
from unittest import mock

import pytest

from tools.ci import perf_nightly

HEAD = "a" * 40


def run(evidence: Path, output: Path, *extra: str) -> int:
    argv = [
        "perf_nightly",
        "--evidence",
        str(evidence),
        "--output",
        str(output),
        *extra,
    ]
    with mock.patch.object(sys, "argv", argv):
        return perf_nightly.main()


def test_a_harness_exception_still_yields_a_failed_night(tmp_path):
    # Independent review finding 6: perf_stack can leave only failure.json,
    # which carries no revision, and the night used to vanish.
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "failure.json").write_text(json.dumps({"type": "RuntimeError"}))
    out = tmp_path / "summary.json"
    assert run(evidence, out, "--revision", HEAD) == 0
    summary = json.loads(out.read_text())
    assert summary["revision"] == HEAD
    assert summary["passed"] is False and summary["failure"] == "RuntimeError"
    assert summary["routes"] == []


def test_no_evidence_at_all_is_still_recorded(tmp_path):
    out = tmp_path / "summary.json"
    assert run(tmp_path / "missing", out, "--revision", HEAD) == 0
    assert json.loads(out.read_text())["failure"] == "no gate result"


def test_the_gate_revision_must_match_the_workflow_revision(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "gate.json").write_text(
        json.dumps(
            {
                "candidate_revision": "b" * 40,
                "baseline_revision": "c" * 40,
                "passed": True,
                "certifying": True,
                "max_p99_ratio": 1.0,
                "comparisons": [],
            }
        )
    )
    assert run(evidence, tmp_path / "s.json", "--revision", HEAD) == 1
    assert not (tmp_path / "s.json").exists()


@pytest.mark.parametrize("revision", ["", "abc", "A" * 40])
def test_the_workflow_revision_must_be_a_full_commit(tmp_path, revision):
    with pytest.raises(SystemExit):
        run(tmp_path, tmp_path / "s.json", "--revision", revision)


def test_the_nightly_workflow_always_summarizes_and_cleans_up():
    workflow = (
        Path(__file__).resolve().parents[2] / ".github/workflows/nightly.yml"
    ).read_text()
    assert '--revision "$head"' in workflow
    assert "trap cleanup EXIT" in workflow
    cleanup = workflow[
        workflow.index("cleanup() {") : workflow.index("trap cleanup EXIT")
    ]
    assert "summarize" in cleanup and "docker image rm" in cleanup
