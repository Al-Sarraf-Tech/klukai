#!/usr/bin/env python3
"""Report measured S criteria; missing checks cannot establish an overall tier."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from checks import evaluate

DIMENSIONS = (
    "code",
    "testing",
    "security",
    "reliability",
    "observability",
    "performance",
    "documentation",
    "process",
)


def source_identity(root: Path) -> tuple[str, bool]:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    )
    if revision.returncode:
        return "UNVERSIONED", False
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return revision.stdout.strip(), not bool(dirty.stdout.strip())


def audit(root: Path, only: str | None, target: str, evidence: Path) -> dict:
    policy = json.loads((Path(__file__).parent / "policy.json").read_text())
    revision, clean = source_identity(root)
    checks = []
    dimensions = {}
    for configured in policy["checks"]:
        if configured.get("target", "S") == "S+" and target != "S+":
            continue
        if only and configured["dim"] != only:
            continue
        try:
            note = evaluate(configured, root, revision, policy, evidence)
            status = "✓"
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.SubprocessError,
        ) as error:
            note = str(error) if isinstance(error, ValueError) else type(error).__name__
            status = "✗"
        checks.append(
            {
                "id": configured["id"],
                "dim": configured["dim"],
                "name": configured["name"],
                "status": status,
                "note": note,
            }
        )
    for dimension in DIMENSIONS:
        selected = [c for c in checks if c["dim"] == dimension]
        passed = sum(c["status"] == "✓" for c in selected)
        failed = len(selected) - passed
        tier = target if selected and not failed and clean else "UNVERIFIED"
        dimensions[dimension] = {
            "tier": tier,
            "pass": passed,
            "fail": failed,
            "evaluated": bool(selected),
        }
    certified = all(d["tier"] == target for d in dimensions.values())
    return {
        "schema_version": 2,
        "target": target,
        "tier": target if certified else "UNVERIFIED",
        "source_revision": revision,
        "working_tree_clean": clean,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "pass": sum(c["status"] == "✓" for c in checks),
        "total": len(checks),
        "dimensions": dimensions,
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--only", choices=DIMENSIONS)
    parser.add_argument("--target", choices=("S", "S+"), default="S")
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=Path(os.environ.get("KLUKAI_AUDIT_EVIDENCE_DIR", ".quality-evidence")),
    )
    parser.add_argument("--diff", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    evidence = (
        args.evidence_dir
        if args.evidence_dir.is_absolute()
        else root / args.evidence_dir
    )
    result = audit(root, args.only, args.target, evidence)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Klukai {args.target} audit — {result['source_revision']}")
        for dimension, value in result["dimensions"].items():
            print(
                f"{dimension:16} {value['tier']:10} {value['pass']} passed / {value['fail']} failed; evaluated={value['evaluated']}"
            )
        for check in result["checks"]:
            print(f"{check['status']} {check['id']} {check['name']}: {check['note']}")
        print(
            f"Overall: {result['tier']}; {result['pass']}/{result['total']} checks passed"
        )
    if args.diff:
        baseline = root / "docs/s-tier-audit-last.json"
        if baseline.exists():
            previous = json.loads(baseline.read_text())
            print(
                f"Previous tier={previous.get('tier')}; current={result['tier']}",
                file=sys.stderr,
            )
        else:
            print("No recorded audit baseline", file=sys.stderr)
    selected_ok = bool(result["checks"]) and all(
        c["status"] == "✓" for c in result["checks"]
    )
    return 0 if selected_ok and (args.only or result["tier"] == args.target) else 1


if __name__ == "__main__":
    raise SystemExit(main())
