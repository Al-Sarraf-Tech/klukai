"""Fail when a tracked file contains an xAI API key.

The key lives in ``~/.config/klukai/xai.env`` on the host. It must not be
committed. Tests build fake keys in pieces so this pattern does not match
the test source.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys

# Built so this source file does not itself contain a key-shaped token.
_KEY = re.compile("xai-" + r"[A-Za-z0-9]{20,}")


def scan_text(text: str) -> bool:
    return _KEY.search(text) is not None


def tracked_hits(root: Path) -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    hits: list[str] = []
    for name in listed.stdout.decode().split("\0"):
        if not name:
            continue
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeError, OSError):
            continue
        if scan_text(text):
            hits.append(name)
    return hits


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    hits = tracked_hits(args.root)
    if hits:
        print("xAI key material in tracked files:", ", ".join(hits), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
