#!/usr/bin/env python3
"""Choose where one CI run's movable jobs go: Dominus when its pool is online.

The Dominus CI controller writes the repository variable
KLUKAI_CI_DOMINUS_HEARTBEAT (epoch seconds) every minute while it can take
runs, and "0" when it cannot (gaming, overnight, unhealthy). A run that starts
within FRESH_SECONDS of a heartbeat sends its build/test chain (ci-image, test,
real-backends), the component suites and the Flutter job to Dominus. amarillo
keeps the quick checks and the image-scan/perf chain: perf measures timing on
its calibrated host, and the release image the deployer reads stays local. The
PWA reaches the deployer through amarillo's evidence pull. Otherwise every job
stays on amarillo. Either way, amarillo's controller takes over a Dominus job
that waits too long.

Jobs that hand files to each other stay in one pool: image-scan's release feeds
perf, and test and real-backends follow the pool that really built ci-image's
executor image (--here). A job amarillo takes over after that waits for the pull
to bring the image. If Dominus is gone the pull cannot, and the run needs "Re-run
all jobs".

Owner, 2026-10-31 and 2026-11-01: this supersedes the amarillo-only decision of
2026-10-26. Klukai's pool stays on both hosts and fans out on Dominus.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

FRESH_SECONDS = 150
# Clock skew between the heartbeat writer and the CI runner.
SKEW_SECONDS = 60
BASE = ["self-hosted", "Linux", "X64"]
# Job groups that may move to Dominus, by the profile kind they run on.
MOVABLE = {"build": "build", "serial": "serial", "frontend": "frontend"}
# One VM per component suite where the budget allows it. On amarillo's 12 vCPUs
# extra VMs would only queue, so it keeps running them together.
COMPONENT_SUITES = {"dominus": ["ops", "voice", "bridge", "js"], "amarillo": ["all"]}
# Each pool's guests route through its private network's gateway.
GATEWAYS = {"10.231.0.1": "amarillo", "10.232.0.1": "dominus"}


def plan(heartbeat: str, now: int) -> dict[str, str]:
    """GITHUB_OUTPUT values: the pool and each movable group's runs-on labels."""
    beat = heartbeat.strip()
    plain = beat.isascii() and beat.isdigit() and len(beat) <= 12
    online = plain and int(beat) > 0 and -SKEW_SECONDS <= now - int(beat) <= FRESH_SECONDS
    pool = "dominus" if online else "amarillo"
    out = {"pool": pool, "component_suites": json.dumps(COMPONENT_SUITES[pool])}
    for group, kind in MOVABLE.items():
        out[f"labels_{group}"] = json.dumps([*BASE, f"klukai-{pool}-{kind}"])
    return out


def here(route: str) -> dict[str, str]:
    """GITHUB_OUTPUT values for jobs that must follow this VM, from `ip -4 route show default`."""
    match = re.search(r"^default via (\S+)", route, re.M)
    pool = GATEWAYS.get(match.group(1)) if match else None
    if pool is None:
        raise SystemExit(f"this VM's gateway belongs to no CI pool: {route.strip()[:80]!r}")
    return {"labels_serial": json.dumps([*BASE, f"klukai-{pool}-serial"])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--heartbeat", default="0")
    parser.add_argument("--now", type=int)
    parser.add_argument("--here", help="this VM's default route, to publish its pool")
    args = parser.parse_args()
    if args.here is None and args.now is None:
        parser.error("--now or --here is required")
    output = here(args.here) if args.here is not None else plan(args.heartbeat, args.now)
    for key, value in output.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
