"""One Klukai production deployment at a time.

The automatic deployer, `klukai-release.py deploy`, and the web release manager
all take this exclusive lock before they change production, so a manual deploy
can never interleave with an automatic one. It is advisory (flock), held for
the whole run, and released by the kernel if the process dies.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path

LOCK = Path.home() / ".local/state/klukai/release/deploy.lock"


class Busy(RuntimeError):
    """Another deployment holds the lock."""


@contextlib.contextmanager
def held(purpose: str, path: Path | None = None) -> Iterator[None]:
    """Hold the deployment lock or raise Busy at once (never queue silently)."""
    path = LOCK if path is None else path
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy(f"another Klukai deployment holds {path}: {holder(path)}") from None
        os.ftruncate(fd, 0)
        os.write(
            fd,
            json.dumps(
                {"pid": os.getpid(), "purpose": purpose, "since": datetime.now(UTC).isoformat()}
            ).encode(),
        )
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the flock


def holder(path: Path | None = None) -> str:
    path = LOCK if path is None else path
    try:
        return path.read_text()[:200] or "unknown"
    except OSError:
        return "unknown"
