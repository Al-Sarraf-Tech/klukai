"""Namespace PID1: admit success only after every child settles naturally."""

from __future__ import annotations

import os
import signal
import sys
from types import FrameType


def interrupted(_signum: int, _frame: FrameType | None) -> None:
    raise InterruptedError("Namespace init interrupted")


def run(argv: list[str]) -> int:
    # This must precede spawning, signal handling and process-wide wait actions.
    if (
        sys.platform != "linux"
        or os.getpid() != 1
        or len(argv) < 2
        or argv[0] != "--"
        or not os.path.isabs(argv[1])
        or any("\0" in value for value in argv)
    ):
        return 2
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, interrupted)
    initial = os.posix_spawn(
        argv[1],
        argv[1:],
        os.environ,
        # Undo Python's ignored signals, matching Popen(restore_signals=True)
        # at the original bwrap exec boundary. Preserve the inherited mask.
        setsigdef=(signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
        + tuple(
            getattr(signal, name)
            for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ")
            if hasattr(signal, name)
        ),
    )
    seen_initial, failed = False, False
    while True:
        try:
            pid, status = os.waitpid(-1, 0)
        except ChildProcessError:
            return 0 if seen_initial and not failed else 1
        if pid == initial:
            seen_initial = True
        failed = failed or os.waitstatus_to_exitcode(status) != 0


def main() -> None:
    try:
        result = run(sys.argv[1:])
    except (Exception, KeyboardInterrupt):
        result = 1
    raise SystemExit(result)


if __name__ == "__main__":
    main()
