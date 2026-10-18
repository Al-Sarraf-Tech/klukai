"""Summarize one nightly perf_stack run into a small, revision-bound record.

The summary is what amarillo pulls into the observability stack. A harness run
that produced no gate result still yields a summary with `passed: false` and the
failure type, so a broken night is visible instead of silently missing. The
revision comes from the workflow, so even a harness that died before writing
any evidence produces a revision-bound failed night.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


def _load(path: Path) -> Any:
    return json.loads(path.read_text()) if path.is_file() else None


def _revision(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise argparse.ArgumentTypeError("a full 40-character revision is required")
    return value


def summarize(evidence: Path, revision: str) -> dict[str, Any]:
    gate = _load(evidence / "gate.json")
    manifest = _load(evidence / "manifest.json") or {}
    failure = _load(evidence / "failure.json")
    summary: dict[str, Any] = {
        "schema": 1,
        "kind": "klukai-perf-nightly",
        "finished_at": manifest.get("finished_at") or datetime.now(UTC).isoformat(),
        "started_at": manifest.get("started_at"),
        "evidence": str(evidence),
    }
    if not isinstance(gate, dict):
        summary.update(
            revision=revision,
            passed=False,
            failure=(failure or {}).get("type", "no gate result"),
            routes=[],
        )
        return summary
    if gate.get("candidate_revision") != revision:
        raise ValueError("gate result is for another revision")
    summary.update(
        revision=revision,
        baseline_revision=gate["baseline_revision"],
        passed=gate["passed"] is True,
        certifying=gate["certifying"] is True,
        max_p99_ratio=gate["max_p99_ratio"],
        noise_floor_ms=gate.get("noise_floor_ms"),
        failures=gate.get("failures", []),
        routes=[
            {
                "method": row["method"],
                "route": row["route"],
                "candidate_p99_ms": row["candidate_p99_ms"],
                "baseline_p99_ms": row["baseline_p99_ms"],
                "p99_ratio": row["p99_ratio"],
            }
            for row in gate["comparisons"]
            if row["round"] == "pooled"
        ],
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", type=_revision, required=True)
    args = parser.parse_args()
    try:
        summary = summarize(args.evidence, args.revision)
    except (ValueError, KeyError, TypeError) as error:
        print(f"nightly summary refused: {error}", file=sys.stderr)
        return 1
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
    print(
        json.dumps({k: summary.get(k) for k in ("revision", "passed", "max_p99_ratio")})
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
