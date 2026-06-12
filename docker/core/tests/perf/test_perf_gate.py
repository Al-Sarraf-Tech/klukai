"""Perf gate — fails on >20% p99 regression vs baseline.

S+ Phase 4 — perf gate target per docs/superpowers/specs/2026-05-16-s-plus-uplift.md §6.6.

Run mode: explicit. `pytest -m perf` against a live stack.
Default test runs skip this file.

Schema note: docs/perf-baseline.json and `probe.py --json` both use the probe
payload schema ``{"results": [{"path", "latency_ms": {"p99"}, ...}]}``. The
first version of this gate read a ``{endpoint: {"p99_ms"}}`` schema that the
probe never produced, so it skipped unconditionally — a hollow gate. The
parsing now goes through tools/load-test/perf_compare.py, the single source
of truth shared with CI.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.perf


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "tools" / "load-test" / "probe.py").exists():
            return parent
    pytest.skip("repo root with tools/load-test not found")


def _load_perf_compare():
    path = _repo_root() / "tools" / "load-test" / "perf_compare.py"
    spec = importlib.util.spec_from_file_location("perf_compare", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def baseline() -> dict:
    """Load the committed perf baseline."""
    baseline_path = _repo_root() / "docs" / "perf-baseline.json"
    if not baseline_path.exists():
        pytest.skip("perf-baseline.json missing — run `make perf-baseline` first")
    return json.loads(baseline_path.read_text())


def test_health_endpoint_p99_within_budget(baseline: dict) -> None:
    """/health p99 within 20% of baseline."""
    pc = _load_perf_compare()
    base_p99 = pc.p99_by_path(baseline).get("/health")
    assert base_p99 is not None, (
        "baseline has no /health p99 — schema drift; regenerate with `make perf-baseline`"
    )
    budget = base_p99 * 1.20

    probe = _repo_root() / "tools" / "load-test" / "probe.py"
    base_url = os.environ.get("KLUKAI_PERF_TARGET", "http://localhost:8300")
    proc = subprocess.run(
        [sys.executable, str(probe), "--base", base_url,
         "--requests", "100", "--concurrency", "5", "--json"],
        capture_output=True, text=True, check=False, timeout=120,
    )
    if proc.returncode != 0:
        pytest.skip(f"probe failed (stack unreachable?): {proc.stderr.strip()[:200]}")

    current = pc.p99_by_path(json.loads(proc.stdout))
    cur_p99 = current.get("/health")
    assert cur_p99 is not None, (
        f"probe emitted no /health p99 — schema drift; stdout: {proc.stdout[:200]}"
    )
    assert cur_p99 <= budget, (
        f"/health p99 regression: current={cur_p99}ms > 1.20×baseline={budget}ms"
    )
