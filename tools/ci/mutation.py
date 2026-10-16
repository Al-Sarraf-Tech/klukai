"""Execute and validate source-bound critical-path mutation evidence."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from .mutation_evidence import (
    tool_identity as tool_identity,
    validate_report as validate_report,
)
from .mutation_runner import execute, git


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "validate"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-children", type=int, choices=(1, 2), default=2)
    parser.add_argument(
        "--development", action="store_true", help="Cannot certify final CI"
    )
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--revision", help="Must match actual checkout HEAD")
    parser.add_argument("--threshold", type=float, default=0.8)
    parser.add_argument("--max-age-days", type=int, default=30)
    args = parser.parse_args()
    try:
        actual_revision = git(args.root, "rev-parse", "HEAD")
        if args.revision is not None and args.revision != actual_revision:
            raise ValueError("Requested revision differs from actual checkout HEAD")
        if not math.isfinite(args.threshold) or not 0.8 <= args.threshold <= 1:
            raise ValueError("Mutation threshold must be between 0.8 and 1")
        if not 1 <= args.max_age_days <= 30:
            raise ValueError("Mutation evidence maximum age must be 1–30 days")
        if args.action == "run":
            result = execute(
                args.root,
                args.output,
                max_children=args.max_children,
                development=args.development,
                timeout=args.timeout,
                threshold=args.threshold,
                max_age_days=args.max_age_days,
            )
        else:
            if git(args.root, "status", "--porcelain") and not args.development:
                raise ValueError("Final CI validation requires a clean exact revision")
            result = validate_report(
                args.output,
                args.root,
                actual_revision,
                threshold=args.threshold,
                max_age_days=args.max_age_days,
                allow_development=args.development,
            )
    except (OSError, ValueError) as error:
        parser.exit(1, f"Mutation gate failed: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
