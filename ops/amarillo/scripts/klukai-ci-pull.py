#!/usr/bin/env python3
"""Pull the CI evidence Dominus's pool wrote into amarillo's tree.

Owner, 2026-10-22: Klukai CI may run jobs on Dominus. Dominus cannot reach
amarillo (Tailscale SSH requires an interactive check there), so amarillo
pulls: every minute, the run records Dominus changed in the last
WINDOW_MINUTES are copied with rsync over ssh, read with sudo on Dominus
because guest-written files keep guest owners. Nothing here is deleted or
overwritten from amarillo's own jobs: each job directory of a run is written
by exactly one pool. The deployer then reads everything locally and still
binds every byte to the run's own job log. Dominus being off is normal; the
next minute tries again.

The link between the hosts is relayed (about 2 MiB/s) and a Dominus run
writes about 1 GB, so each pull copies the frontend job first (the PWA the
deployer reads, about 70 MB) and then gives the rest at most BULK_SECONDS.
An interrupted file resumes from .rsync-partial on the next pull, so a big
record never holds the deployer's PWA back for more than about a minute.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable

HOST = "dominus-nobara"
REMOTE = "/srv/klukai-ci/artifacts"
LOCAL = Path("/docker/klukai/ci-artifacts")
# Longer than any CI job, shorter than amarillo's two-hour archive idle time,
# so an archived record is never pulled back.
WINDOW_MINUTES = 90
RECORD = re.compile(r"[0-9a-f]{40}/[0-9]+-[0-9]+")
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
# The job directory the deployer reads from a Dominus run.
PRIORITY = ["--include=/*/*/frontend/***", "--exclude=/*/*/*"]
BULK_SECONDS = 45
# 24: a file vanished mid-copy (pruned or rewritten).
FINE = (0, 24)


def recent(run: Callable[..., Any] = subprocess.run) -> list[str] | None:
    """Run records Dominus changed lately, or None when Dominus is unreachable."""
    found = run(
        [*SSH, HOST, f"sudo -n find {REMOTE} -mindepth 2 -maxdepth 2 -type d "
                     f"-mmin -{WINDOW_MINUTES} -printf '%P\\n'"],
        capture_output=True, text=True, timeout=60,
    )
    if found.returncode == 255:
        return None
    if found.returncode:
        raise RuntimeError("could not list Dominus's CI records")
    return sorted({p for p in found.stdout.split() if RECORD.fullmatch(p)})


def pull(records: list[str], run: Callable[..., Any] = subprocess.run, local: Path = LOCAL) -> None:
    """Copy those records: the frontend jobs in full, then the rest for BULK_SECONDS."""
    if not records:
        return
    listing = "".join(r + "\n" for r in records)
    rsync = ["rsync", "-rlt", "--safe-links", "--chmod=D700,F600", "--partial-dir=.rsync-partial",
             "--rsync-path=sudo -n rsync", "--files-from=-",
             "-e", " ".join(SSH[:-1] + ["ConnectTimeout=10"]), f"{HOST}:{REMOTE}/", f"{local}/"]
    first = run([rsync[0], *PRIORITY, *rsync[1:]], input=listing, capture_output=True, text=True,
                timeout=900)
    if first.returncode not in FINE:
        raise RuntimeError(f"rsync failed with exit {first.returncode}")
    # TERM, not KILL, so rsync keeps the part it copied for the next pull.
    rest = run(["timeout", "--signal=TERM", "--kill-after=10", str(BULK_SECONDS), *rsync],
               input=listing, capture_output=True, text=True, timeout=BULK_SECONDS + 30)
    if rest.returncode not in (*FINE, 124):
        raise RuntimeError(f"rsync failed with exit {rest.returncode}")


def main() -> int:
    try:
        records = recent()
        if records is None:
            print(json.dumps({"pulled": [], "skipped": "Dominus unreachable"}))
            return 0
        pull(records)
        print(json.dumps({"pulled": records}))
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"klukai-ci-pull: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
