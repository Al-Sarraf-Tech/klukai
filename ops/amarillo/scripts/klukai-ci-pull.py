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
    """Copy those records; partial files are completed by a later pull."""
    if not records:
        return
    result = run(
        ["rsync", "-rlt", "--safe-links", "--rsync-path=sudo -n rsync", "--files-from=-",
         "-e", " ".join(SSH[:-1] + ["ConnectTimeout=10"]), f"{HOST}:{REMOTE}/", f"{local}/"],
        input="".join(r + "\n" for r in records), capture_output=True, text=True, timeout=1800,
    )
    if result.returncode not in (0, 24):  # 24: a file vanished mid-copy (pruned or rewritten)
        raise RuntimeError(f"rsync failed with exit {result.returncode}")


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
