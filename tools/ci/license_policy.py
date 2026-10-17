"""Enforce the owner's licence policy on a Trivy licence inventory of the image.

Allowed: Trivy's notice/permissive/unencumbered categories, the GPL/LGPL family
(owner decision 2026-10-17: private, never-distributed service), and licences
reviewed per package or per identifier in license_policy.json. Anything else,
including an unclassified licence nobody reviewed, fails the build.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any

POLICY = Path(__file__).with_name("license_policy.json")


def findings(inventory: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    for result in inventory.get("Results") or []:
        for item in result.get("Licenses") or []:
            rows.append(
                {
                    "package": str(item.get("PkgName") or item.get("FilePath") or ""),
                    "license": str(item.get("Name") or ""),
                    "category": str(item.get("Category") or "unknown"),
                }
            )
    return rows


def judge(inventory: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    if policy.get("schema") != 1:
        raise ValueError("Unknown licence policy schema")
    rows = findings(inventory)
    if not rows:
        raise ValueError("Licence inventory is empty; refusing to certify")
    copyleft = set(policy["allowed_copyleft"])
    categories = set(policy["allowed_categories"])
    reviewed = policy["reviewed_identifiers"]
    packages = {k: set(v) for k, v in policy["reviewed_packages"].items()}
    violations = []
    for row in rows:
        name, package = row["license"], row["package"]
        if (
            row["category"] in categories
            or name in copyleft
            or name in reviewed
            or package in packages.get(name, ())
        ):
            continue
        violations.append(row)
    return {
        "schema": 1,
        "policy_decided": policy["decided"],
        "findings": len(rows),
        "categories": dict(Counter(row["category"] for row in rows)),
        "violations": violations,
        "passed": not violations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path, help="trivy --scanners license JSON")
    parser.add_argument("--policy", type=Path, default=POLICY)
    parser.add_argument("--output", type=Path, help="write the verdict JSON here")
    args = parser.parse_args()
    try:
        verdict = judge(
            json.loads(args.inventory.read_text()), json.loads(args.policy.read_text())
        )
    except (OSError, ValueError, KeyError) as error:
        print(f"licence policy refused: {error}", file=sys.stderr)
        return 1
    text = json.dumps(verdict, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(text + "\n")
    for row in verdict["violations"]:
        print(
            f"::error title=Licence not allowed::{row['package']}: "
            f"{row['license']} ({row['category']})",
            file=sys.stderr,
        )
    print(
        f"licence policy: {verdict['findings']} findings, "
        f"{len(verdict['violations'])} violations"
    )
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
