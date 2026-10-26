#!/usr/bin/env python3
"""Publish only a gaming boolean and timestamp; never process details."""

import json
import os
import time
from pathlib import Path


def detect(proc=Path("/proc")):
    for path in proc.glob("[0-9]*/environ"):
        try:
            # Inspect only the current owner's processes; nothing is logged.
            if path.stat().st_uid != os.getuid():
                continue
            fields = path.read_bytes().split(b"\0")
            if any(
                f.startswith((b"SteamAppId=", b"SteamGameId="))
                and f.split(b"=", 1)[1] not in (b"", b"0")
                for f in fields
            ):
                return True
            comm = (path.parent / "comm").read_text().strip().lower()
            if comm in {"cs2", "dota2", "hl2_linux", "minecraft", "osu!", "gamescope"}:
                return True
        except (OSError, ValueError):
            continue
    return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    while True:
        target = args.output
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"game_active": detect(), "updated_at": time.time()})
        )
        temporary.chmod(0o644)
        temporary.replace(target)
        time.sleep(10)
