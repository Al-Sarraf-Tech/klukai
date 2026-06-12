"""Unit tests for tools/load-test/perf_compare.py — the p99 regression gate.

The gate was previously hollow: CI and test_perf_gate.py read a
``{endpoint: {p99_ms}}`` schema while probe.py writes
``{"results": [{"path", "latency_ms": {"p99"}}]}``, so zero endpoints were
ever compared. These tests pin the gate to the real probe schema.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

def _find_perf_compare() -> Path:
    """Walk upward to the repo root so this also resolves under mutmut's mutants/ copy."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "tools" / "load-test" / "perf_compare.py"
        if candidate.exists():
            return candidate
    return Path("tools/load-test/perf_compare.py")


PERF_COMPARE = _find_perf_compare()


def _payload(p99_by_path: dict[str, float]) -> dict:
    return {
        "base_url": "http://localhost:8300",
        "results": [
            {
                "label": path.strip("/").replace("/", "-") or "root",
                "method": "GET",
                "path": path,
                "requests": 100,
                "errors": 0,
                "error_rate": 0.0,
                "latency_ms": {"p50": p99 / 2, "p95": p99 * 0.9, "p99": p99, "mean": p99 / 2},
            }
            for path, p99 in p99_by_path.items()
        ],
    }


def _run(tmp_path: Path, baseline: dict, current: dict, *extra: str) -> subprocess.CompletedProcess:
    b = tmp_path / "baseline.json"
    c = tmp_path / "current.json"
    b.write_text(json.dumps(baseline))
    c.write_text(json.dumps(current))
    return subprocess.run(
        [sys.executable, str(PERF_COMPARE), str(b), str(c), *extra],
        capture_output=True, text=True, timeout=30,
    )


class TestPerfCompare:
    def test_script_exists(self):
        assert PERF_COMPARE.exists(), f"missing {PERF_COMPARE}"

    def test_within_budget_passes(self, tmp_path):
        proc = _run(tmp_path, _payload({"/health": 100.0}), _payload({"/health": 110.0}))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "/health" in proc.stdout

    def test_regression_fails(self, tmp_path):
        proc = _run(tmp_path, _payload({"/health": 100.0}), _payload({"/health": 121.0}))
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "REGRESSION" in proc.stdout.upper()

    def test_exactly_at_threshold_passes(self, tmp_path):
        proc = _run(tmp_path, _payload({"/health": 100.0}), _payload({"/health": 120.0}))
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_custom_threshold(self, tmp_path):
        proc = _run(
            tmp_path, _payload({"/health": 100.0}), _payload({"/health": 130.0}),
            "--threshold", "1.5",
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_endpoint_missing_from_current_fails(self, tmp_path):
        """A baseline endpoint the probe no longer measures must not pass silently."""
        baseline = _payload({"/health": 100.0, "/api/health/subsystems": 150.0})
        current = _payload({"/health": 100.0})
        proc = _run(tmp_path, baseline, current)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "subsystems" in proc.stdout

    def test_zero_comparable_endpoints_fails(self, tmp_path):
        """Schema drift (nothing comparable) must fail loudly, not pass vacuously."""
        proc = _run(tmp_path, {"results": []}, _payload({"/health": 100.0}))
        assert proc.returncode == 1, proc.stdout + proc.stderr

    def test_legacy_flat_schema_rejected(self, tmp_path):
        """The old hollow-gate schema must be rejected, not silently compared as zero endpoints."""
        legacy = {"/health": {"p99_ms": 100.0}}
        proc = _run(tmp_path, legacy, _payload({"/health": 100.0}))
        assert proc.returncode == 1, proc.stdout + proc.stderr

    def test_multiple_endpoints_reports_each(self, tmp_path):
        baseline = _payload({"/health": 100.0, "/api/health/subsystems": 200.0})
        current = _payload({"/health": 90.0, "/api/health/subsystems": 500.0})
        proc = _run(tmp_path, baseline, current)
        assert proc.returncode == 1
        assert "/api/health/subsystems" in proc.stdout
        assert "/health" in proc.stdout


class TestProbeJsonFlag:
    """probe.py must support --json (stdout payload) for the perf gate test."""

    def test_probe_json_flag_exists(self):
        probe = PERF_COMPARE.parent / "probe.py"
        proc = subprocess.run(
            [sys.executable, str(probe), "--help"],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0
        assert "--json" in proc.stdout
