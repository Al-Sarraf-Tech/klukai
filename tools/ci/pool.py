#!/usr/bin/env python3
"""Choose where one CI run's movable jobs go: Dominus when its pool is online.

The Dominus CI controller writes the repository variable
KLUKAI_CI_DOMINUS_HEARTBEAT (epoch seconds) every minute while it can take
runs, and "0" when it cannot (gaming, overnight, unhealthy). A run that starts
within FRESH_SECONDS of a heartbeat sends its build/test chain (ci-image, test,
real-backends), the component suites and the Flutter job to Dominus. amarillo
keeps the quick checks and the image-scan/perf chain: perf measures timing on
its calibrated host, and the release artifacts the deployer reads stay local.
Otherwise every job stays on amarillo. Either way, amarillo's controller takes
over a Dominus job that waits too long, so a run never strands.

Jobs that hand files to each other stay in one pool: ci-image's executor image
feeds test and real-backends, and image-scan's release feeds perf.
"""

from __future__ import annotations

import argparse
import json
import sys

FRESH_SECONDS = 150
# Clock skew between the heartbeat writer and the CI runner.
SKEW_SECONDS = 60
BASE = ["self-hosted", "Linux", "X64"]
# Job groups that may move to Dominus, by the profile kind they run on.
MOVABLE = {"build": "build", "serial": "serial", "frontend": "frontend"}


def plan(heartbeat: str, now: int) -> dict[str, str]:
    """GITHUB_OUTPUT values: the pool and each movable group's runs-on labels."""
    beat = heartbeat.strip()
    online = beat.isdigit() and int(beat) > 0 and -SKEW_SECONDS <= now - int(beat) <= FRESH_SECONDS
    pool = "dominus" if online else "amarillo"
    out = {"pool": pool}
    for group, kind in MOVABLE.items():
        out[f"labels_{group}"] = json.dumps([*BASE, f"klukai-{pool}-{kind}"])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--heartbeat", default="0")
    parser.add_argument("--now", type=int, required=True)
    args = parser.parse_args()
    for key, value in plan(args.heartbeat, args.now).items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
