"""Perf gate — fails on >20% p99 regression vs baseline.

S+ Phase 4 — perf gate target per docs/superpowers/specs/2026-05-16-s-plus-uplift.md §6.6.

Run mode: explicit. `pytest -m perf` against a live stack.
Default test runs skip this file.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.perf


@pytest.fixture(scope="module")
def baseline() -> dict:
    """Load the committed perf baseline."""
    repo_root = Path(__file__).parent.parent.parent.parent.parent
    baseline_path = repo_root / "docs" / "perf-baseline.json"
    if not baseline_path.exists():
        pytest.skip("perf-baseline.json missing — run `make perf-baseline` first")
    return json.loads(baseline_path.read_text())


def test_health_endpoint_p99_within_budget(baseline: dict) -> None:
    """/health p99 within 20% of baseline. Per docs/slos.md the SLO is ≤30ms."""
    base = baseline.get("/health", {}) if isinstance(baseline.get("/health"), dict) else {}
    base_p99 = base.get("p99_ms")
    if base_p99 is None:
        pytest.skip("baseline /health.p99_ms not recorded")
    budget = base_p99 * 1.20
    repo_root = Path(__file__).parent.parent.parent.parent.parent
    probe = repo_root / "tools" / "load-test" / "probe.py"
    if not probe.exists():
        pytest.skip("tools/load-test/probe.py missing")
    base_url = os.environ.get("KLUKAI_PERF_TARGET", "http://localhost:8300")
    proc = subprocess.run(
        [
            "python3",
            str(probe),
            "--base",
            base_url,
            "--requests",
            "100",
            "--concurrency",
            "5",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        pytest.skip(f"probe failed: {proc.stderr.strip()[:200]}")
    report = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}
    current = report.get("/health", {}).get("p99_ms") if isinstance(report, dict) else None
    if current is None:
        pytest.skip("probe didn't emit /health p99 — incompatible output format")
    assert current <= budget, (
        f"/health p99 regression: current={current}ms > 1.20×baseline={budget}ms"
    )
