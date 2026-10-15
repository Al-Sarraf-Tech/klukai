"""Missing scan/performance capabilities must fail the workflow job."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def scripts(job: str) -> str:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    return "\n".join(step.get("run", "") for step in workflow["jobs"][job]["steps"])


def test_image_scan_cannot_report_success_when_docker_is_missing():
    script = scripts("image-scan")
    assert "exit 0" not in script, "Missing Docker currently turns the scan green"
    assert "--ignore-unfixed" not in script, (
        "Unfixed vulnerabilities still require review"
    )
    assert "--exit-code 1" in script


def test_performance_gate_requires_samples_and_enforces_twenty_percent_budget():
    script = scripts("perf-gate")
    assert "exit 0" not in script, (
        "Missing target/samples currently turn performance green"
    )
    assert "--threshold 1.20" in script
    assert "--threshold 2.0" not in script
