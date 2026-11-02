#!/usr/bin/env python3
"""Pull the CI evidence Dominus's pool wrote into amarillo's tree.

Owner, 2026-10-22: Klukai CI may run jobs on Dominus. Dominus cannot reach
amarillo (Tailscale SSH requires an interactive check there), so amarillo
pulls every minute with rsync over ssh, read with sudo on Dominus because
guest-written files keep guest owners. Nothing here is deleted or overwritten
from amarillo's own jobs: each job directory of a run is written by exactly
one pool. The deployer then reads everything locally and still binds every
byte to the run's own job log. Dominus being off is normal; the next minute
tries again.

The link between the hosts is relayed (about 2 MiB/s), a Dominus run writes
about 1 GB and its record holds about 6,600 files, so:
- Dominus reports a fingerprint per record (files, bytes, newest mtime) for
  the two days it keeps them. A record is copied again only when it changed
  since its last complete copy (the ledger in STATE).
- Each pull copies the frontend job first (the PWA the deployer reads, about
  70 MB), then gives the rest at most BULK_SECONDS, one record at a time and
  oldest first. Interrupted files resume from .rsync-partial on the next pull.
- Directories get local times, so amarillo's archiver (two hours idle) never
  packs a revision while its files are still arriving. A revision it has
  already packed and removed is never pulled back: that would overwrite its
  archive with a partial one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

HOST = "dominus-nobara"
REMOTE = "/srv/klukai-ci/artifacts"
LOCAL = Path("/docker/klukai/ci-artifacts")
ARCHIVE = Path("/docker/klukai/ci-records")
STATE = Path.home() / ".local/state/klukai/ci-pull.json"
# Dominus prunes a record two days after its last change (host.json record_days).
LOOKBACK_MINUTES = 2 * 24 * 60
RECORD = re.compile(r"([0-9a-f]{40})/[0-9]+-[0-9]+")
FINGERPRINT = re.compile(r"[0-9]+:[0-9]+:[0-9]+(\.[0-9]+)?")
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
# "<files>:<bytes>:<newest mtime> <record>" for every record changed lately.
LIST = r"""cd "$1" || exit 1
find . -mindepth 2 -maxdepth 2 -type d -mmin "-$2" -printf '%P\n' | while IFS= read -r r; do
  printf '%s %s\n' "$(find "$r" -printf '%T@ %s\n' |
    awk '{n++; b+=$2; if ($1>m) m=$1} END {printf "%d:%d:%.6f", n, b, m}')" "$r"
done
"""
# The job directory the deployer reads from a Dominus run, and the executor image
# a test job amarillo took over from Dominus is waiting for.
PRIORITY = ["--include=/*/*/frontend/***", "--include=/*/*/ci-tool-image/***", "--exclude=/*/*/*"]
BULK_SECONDS = 45
# Too little time left to be worth starting another record's copy.
MIN_SLICE_SECONDS = 5
# 24: a file vanished mid-copy (pruned or rewritten).
FINE = (0, 24)


def recent(run: Callable[..., Any] = subprocess.run) -> dict[str, str] | None:
    """Dominus's records with their fingerprints, or None when Dominus is unreachable."""
    command = f"sudo -n sh -c {shlex.quote(LIST)} sh {REMOTE} {LOOKBACK_MINUTES}"
    found = run([*SSH, HOST, command], capture_output=True, text=True, timeout=120)
    if found.returncode == 255:
        return None
    if found.returncode:
        raise RuntimeError("could not list Dominus's CI records")
    listing = {}
    for line in found.stdout.splitlines():
        fingerprint, _, record = line.partition(" ")
        if RECORD.fullmatch(record) and FINGERPRINT.fullmatch(fingerprint):
            listing[record] = fingerprint
    return listing


def todo(listing: dict[str, str], synced: dict[str, str], local: Path = LOCAL,
         archive: Path = ARCHIVE) -> list[str]:
    """Records changed since their last complete copy, oldest first."""
    def packed(record: str) -> bool:
        revision = record.split("/")[0]
        return (archive / f"{revision}.tar").exists() and not (local / revision).exists()

    wanted = [r for r, fp in listing.items() if synced.get(r) != fp and not packed(r)]
    return sorted(wanted, key=lambda r: (float(listing[r].rsplit(":", 1)[1]), r))


def pull(records: list[str], run: Callable[..., Any] = subprocess.run, local: Path = LOCAL,
         clock: Callable[[], float] = time.monotonic) -> list[str]:
    """Copy those records: frontend jobs in full, then the rest for BULK_SECONDS.

    The rest goes one record at a time, oldest first, so each record carries only
    its own file list and counts as copied as soon as its own copy finishes.
    Returns the records copied in full.
    """
    if not records:
        return []
    rsync = ["rsync", "-rlt", "--omit-dir-times", "--safe-links", "--chmod=D700,F600",
             "--partial-dir=.rsync-partial", "--rsync-path=sudo -n rsync", "--files-from=-",
             "-e", " ".join(SSH[:-1] + ["ConnectTimeout=10"]), f"{HOST}:{REMOTE}/", f"{local}/"]
    first = run([rsync[0], *PRIORITY, *rsync[1:]], input="".join(r + "\n" for r in records),
                capture_output=True, text=True, timeout=900)
    if first.returncode not in FINE:
        raise RuntimeError(f"rsync failed with exit {first.returncode}")
    start, completed = clock(), []
    for record in records:
        left = int(BULK_SECONDS - (clock() - start))
        if left < MIN_SLICE_SECONDS:
            break
        # TERM, not KILL, so rsync keeps the part it copied for the next pull.
        rest = run(["timeout", "--signal=TERM", "--kill-after=10", str(left), *rsync],
                   input=record + "\n", capture_output=True, text=True, timeout=left + 30)
        if rest.returncode == 124:
            break
        if rest.returncode not in FINE:
            raise RuntimeError(f"rsync failed with exit {rest.returncode}")
        completed.append(record)
    return completed


def load(state: Path = STATE) -> dict[str, str]:
    try:
        doc = json.loads(state.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in doc.get("synced", {}).items() if isinstance(v, str)}


def save(synced: dict[str, str], state: Path = STATE) -> None:
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=state.parent)
    with os.fdopen(fd, "w") as out:
        json.dump({"synced": synced}, out, sort_keys=True)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, state)


def main(run: Callable[..., Any] = subprocess.run, *, local: Path = LOCAL,
         archive: Path = ARCHIVE, state: Path = STATE) -> int:
    try:
        listing = recent(run)
        if listing is None:
            print(json.dumps({"pulled": [], "skipped": "Dominus unreachable"}))
            return 0
        # Records Dominus no longer lists were pruned there: forget them.
        synced = {r: fp for r, fp in load(state).items() if r in listing}
        wanted = todo(listing, synced, local, archive)
        completed = pull(wanted, run, local)
        # The fingerprints were taken before the copy: anything written
        # meanwhile changes them, so that record is copied again.
        synced.update({r: listing[r] for r in completed})
        save(synced, state)
        print(json.dumps({"pending": len(wanted) - len(completed), "completed": completed}))
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"klukai-ci-pull: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
