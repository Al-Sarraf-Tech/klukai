#!/usr/bin/env python3
"""Compare two probe.py snapshots and fail on p99 regression.

This is the single source of truth for the perf gate. CI (ci.yml perf-gate,
nightly.yml perf-baseline) and tests/perf/test_perf_gate.py all parse the
probe.py payload schema through here:

    {"results": [{"path": "/health", "latency_ms": {"p99": 130.74}, ...}]}

The previous inline gate scripts expected ``{endpoint: {"p99_ms": ...}}``,
which the probe never produced — so the gate compared zero endpoints and
passed vacuously. This script fails loudly when nothing is comparable.

Usage:
    perf_compare.py baseline.json current.json [--threshold 1.20]

Exit codes: 0 ok, 1 regression / missing endpoint / schema drift.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def p99_by_path(payload: dict) -> dict[str, float]:
    """Extract {path: p99_ms} from a probe.py payload. Empty dict on drift."""
    out: dict[str, float] = {}
    for entry in payload.get("results", []) if isinstance(payload, dict) else []:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        p99 = (entry.get("latency_ms") or {}).get("p99")
        if isinstance(path, str) and isinstance(p99, (int, float)):
            out[path] = float(p99)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("baseline", type=Path)
    ap.add_argument("current", type=Path)
    ap.add_argument("--threshold", type=float, default=1.20,
                    help="max allowed current/baseline p99 ratio (default 1.20)")
    args = ap.parse_args()

    base = p99_by_path(json.loads(args.baseline.read_text()))
    cur = p99_by_path(json.loads(args.current.read_text()))

    if not base:
        print("FAIL: baseline contains no comparable endpoints "
              "(expected probe.py schema with results[].latency_ms.p99)")
        return 1

    failures = []
    for path, base_p99 in sorted(base.items()):
        cur_p99 = cur.get(path)
        if cur_p99 is None:
            failures.append(f"{path}: missing from current snapshot")
            print(f"  {path:32s} baseline={base_p99:8.2f}ms  current=MISSING")
            continue
        budget = base_p99 * args.threshold
        status = "ok" if cur_p99 <= budget else "REGRESSION"
        print(f"  {path:32s} baseline={base_p99:8.2f}ms  current={cur_p99:8.2f}ms  "
              f"budget={budget:8.2f}ms  {status}")
        if cur_p99 > budget:
            failures.append(
                f"{path}: p99 {cur_p99:.2f}ms > {budget:.2f}ms "
                f"({args.threshold:.0%} of baseline {base_p99:.2f}ms)"
            )

    if failures:
        print("PERF REGRESSION:")
        for f in failures:
            print(f"  {f}")
        return 1
    print(f"perf within budget ({len(base)} endpoint(s) compared)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
