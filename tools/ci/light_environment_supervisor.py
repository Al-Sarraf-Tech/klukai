"""A dedicated Linux subreaper owns and finitely cleans one command tree."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
from types import FrameType
from typing import Any

CLEANUP_SECONDS = 2.0
PARENT_VARIABLE = "KLUKAI_COMMAND_PARENT"


def capabilities() -> dict[str, bool]:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER, this process only.
        raise OSError(ctypes.get_errno(), "Cannot establish command subreaper")
    value = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(value), 0, 0, 0) != 0 or value.value != 1:
        raise OSError("Command subreaper capability not verified")
    descriptor = os.pidfd_open(os.getpid())
    try:
        signal.pidfd_send_signal(descriptor, 0)
    finally:
        os.close(descriptor)
    return {"subreaper": True, "pidfd": True}


def children() -> list[int]:
    return [
        int(raw)
        for raw in Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
    ]


def identity(pid: int) -> tuple[int, int]:
    fields = Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2].split()
    return int(fields[1]), int(fields[19])  # PPID and kernel start ticks.


def parent_handle(record: dict[str, Any]) -> int:
    expected = os.environ.get(PARENT_VARIABLE, "")
    pid, ticks = map(int, expected.split(":"))
    if pid < 1 or os.getppid() != pid or identity(pid)[1] != ticks:
        raise RuntimeError("Original command caller identity changed")
    descriptor = os.pidfd_open(pid)
    try:
        if os.getppid() != pid or identity(pid)[1] != ticks:
            raise RuntimeError("Original command caller changed during acquisition")
        record["parent_identity"] = {"pid": pid, "start_ticks": ticks}
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def kill_owned(pid: int, record: dict[str, Any]) -> None:
    descriptor: int | None = None
    try:
        before = identity(pid)
        if before[0] != os.getpid():
            raise RuntimeError("Command child ownership changed")
        descriptor = os.pidfd_open(pid)
        if identity(pid) != before:
            raise RuntimeError("Command child identity changed before signalling")
        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        record["terminated"].append(
            {"pid": pid, "start_ticks": before[1], "pidfd": True}
        )
    except (ProcessLookupError, FileNotFoundError):
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def reap(record: dict[str, Any], child: subprocess.Popen[bytes] | None) -> bool:
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        if not pid:
            return False
        exit_code = os.waitstatus_to_exitcode(status)
        record["reaped"].append({"pid": pid, "exit_code": exit_code})
        if child is not None and pid == child.pid:
            child.returncode = exit_code
            record["exit_code"] = exit_code


def cleanup(record: dict[str, Any], child: subprocess.Popen[bytes] | None) -> None:
    end = time.monotonic() + CLEANUP_SECONDS
    while time.monotonic() < end:
        if reap(record, child):
            record["cleanup_complete"] = True
            return
        for pid in children():
            try:
                kill_owned(pid, record)
            except (OSError, RuntimeError) as error:
                record["cleanup_errors"].append(str(error))
        time.sleep(0.005)
    record["cleanup_complete"] = reap(record, child)
    record["remaining_children"] = children()


def monitor(
    record: dict[str, Any],
    child: subprocess.Popen[bytes],
    timeout: float,
    stop: Path,
    parent_descriptor: int,
) -> None:
    end = time.monotonic() + timeout
    finished = False
    while not record["signals"] and not stop.exists() and time.monotonic() < end:
        record["parent_dead"] = bool(select.select([parent_descriptor], [], [], 0)[0])
        if record["parent_dead"]:
            break
        if reap(record, child):
            finished = True
            break
        time.sleep(0.005)
    record["parent_stop"] = stop.exists()
    if (
        not finished
        and not record["signals"]
        and not record["parent_stop"]
        and not record["parent_dead"]
    ):
        if record["exit_code"] is None:
            record["timed_out"] = True
        else:
            record["orphaned_descendants"] = True


def supervise(argv: list[str], timeout: float, report: Path) -> int:
    record: dict[str, Any] = {
        "argv": argv,
        "exit_code": None,
        "cleanup_complete": False,
        "terminated": [],
        "reaped": [],
        "cleanup_errors": [],
        "signals": [],
        "capabilities": {},
    }

    def receive(signum: int, _frame: FrameType | None) -> None:
        record["signals"].append(signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, receive)
    child: subprocess.Popen[bytes] | None = None
    parent_descriptor: int | None = None
    try:
        record["capabilities"] = capabilities()
        parent_descriptor = parent_handle(record)
        record["parent_stop"] = report.with_suffix(".cancel").exists()
        record["parent_dead"] = bool(select.select([parent_descriptor], [], [], 0)[0])
        if not record["parent_stop"] and not record["parent_dead"]:
            child = subprocess.Popen(argv, start_new_session=True)
            record["command_pid"] = child.pid
            monitor(
                record, child, timeout, report.with_suffix(".cancel"), parent_descriptor
            )
    except (Exception, KeyboardInterrupt) as error:
        record["error_type"], record["error"] = type(error).__name__, str(error)
    finally:
        try:
            cleanup(record, child)
            if parent_descriptor is not None:
                record["parent_dead"] = record.get("parent_dead", False) or bool(
                    select.select([parent_descriptor], [], [], 0)[0]
                )
        except (Exception, KeyboardInterrupt) as error:
            record["cleanup_complete"] = False
            record["cleanup_errors"].append(str(error))
        finally:
            if parent_descriptor is not None:
                os.close(parent_descriptor)
        passed = (
            record["exit_code"] == 0
            and record["cleanup_complete"] is True
            and not record["cleanup_errors"]
            and not record["signals"]
            and not record.get("error")
            and not record.get("timed_out")
            and not record.get("orphaned_descendants")
            and not record.get("parent_stop")
            and not record.get("parent_dead")
            and all(row["exit_code"] == 0 for row in record["reaped"])
        )
        record["passed"] = passed
        temporary = report.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        temporary.replace(report)
    return 0 if passed else 1


def main() -> None:
    raise SystemExit(supervise(sys.argv[3:], float(sys.argv[2]), Path(sys.argv[1])))


if __name__ == "__main__":
    main()
