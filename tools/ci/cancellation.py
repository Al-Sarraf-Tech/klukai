"""Latch process cancellation; defer delivery while reaping and recording evidence.

Only the first normal-phase signal interrupts work. Protected phases record both
SIGTERM and SIGINT without raising; callers finalize a failed report if either
arrived. Nested resource helpers share the entrypoint's latch.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
import signal
from types import FrameType
from typing import Any

from . import core_source


class Cancelled(RuntimeError):
    """The run was cancelled before its terminal evidence was committed."""


class SignalLatch:
    def __init__(self) -> None:
        self.signals: list[int] = []
        self.depth = 0

    @property
    def requested(self) -> bool:
        return bool(self.signals)

    def handle(self, signum: int, _frame: FrameType | None) -> None:
        first = not self.signals
        self.signals.append(signum)
        if first and not self.depth:
            raise Cancelled(f"CI cancelled by signal {signum}")

    def checkpoint(self) -> None:
        if self.requested and not self.depth:
            raise Cancelled(f"CI cancelled by signal {self.signals[0]}")

    @contextmanager
    def deferred(self) -> Iterator[None]:
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1

    def mark(self, report: dict[str, Any]) -> None:
        report["cancelled"] = self.requested
        report["signals"] = list(self.signals)
        if self.requested:
            report["passed"] = report["certifying"] = False
            report.setdefault("error_type", "Cancelled")
            report.setdefault("error", f"CI cancelled by signal {self.signals[0]}")

    def write_report(self, path: Path, report: dict[str, Any]) -> None:
        with self.deferred():
            self.mark(report)
            core_source.write_json(path, report)
            # A first signal can arrive after serialization, during the write.
            # Once failed, further signals cannot turn the terminal result green.
            if self.requested and not report["cancelled"]:
                self.mark(report)
                core_source.write_json(path, report)


_active: SignalLatch | None = None


@contextmanager
def cancellation_scope() -> Iterator[SignalLatch]:
    global _active
    if _active is not None:
        yield _active
        return
    latch = SignalLatch()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    _active = latch
    try:
        for sig in previous:
            signal.signal(sig, latch.handle)
        yield latch
    finally:
        with latch.deferred():
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        _active = None


def checkpoint() -> None:
    if _active is not None:
        _active.checkpoint()
