#!/usr/bin/env python3
"""Route Klukai CI to Amarillo only (owner decision, 2026-10-26).

Legacy command arguments are accepted during rollout but never select another
host. A stale repository variable cannot restore the retired Dominus pool.
"""

from __future__ import annotations

import argparse
import json

BASE = ["self-hosted", "Linux", "X64"]
MOVABLE = {"build": "build", "serial": "serial", "frontend": "frontend"}


def plan(heartbeat: str = "0", now: int = 0) -> dict[str, str]:
    """GITHUB_OUTPUT values for the existing Amarillo job profiles."""
    return {
        "pool": "amarillo",
        **{f"labels_{group}": json.dumps([*BASE, f"klukai-amarillo-{kind}"])
           for group, kind in MOVABLE.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--heartbeat", default="0")
    parser.add_argument("--now", type=int, default=0)
    args = parser.parse_args()
    for key, value in plan(args.heartbeat, args.now).items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
