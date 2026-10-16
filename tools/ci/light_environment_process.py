"""Private command logs and finite pidfd-controlled subreaper supervision."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import Any

from . import cancellation, light_environment_supervisor as supervisor
from .cancellation import cancellation_scope
from .core_source import require, sha256, write_json


class Commands:
    def __init__(self, output: Path, timeout: float) -> None:
        require(math.isfinite(timeout) and 0 < timeout <= 1800, "Invalid timeout")
        require(
            hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"),
            "Native pidfd capability is required",
        )
        descriptor = os.pidfd_open(os.getpid())
        try:
            signal.pidfd_send_signal(descriptor, 0)
        finally:
            os.close(descriptor)
        self.output, self.timeout = output, timeout
        self.records: list[dict[str, Any]] = []
        home = output / "home"
        home.mkdir(mode=0o700)
        self.environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
            "LC_ALL": "C.UTF-8",
            "PIP_CONFIG_FILE": "/dev/null",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SOURCE_DATE_EPOCH": "0",
        }

    def stop(
        self,
        child: subprocess.Popen[str],
        descriptor: int | None,
        record: dict[str, Any],
    ) -> None:
        if child.returncode is not None:
            return
        (self.output / str(record["supervisor_report"])).with_suffix(
            ".cancel"
        ).write_text("stop\n")
        if descriptor is not None:
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            child.wait(timeout=supervisor.CLEANUP_SECONDS + 1)
        except subprocess.TimeoutExpired:
            record["supervisor_deadline_exceeded"] = True
            if descriptor is None:
                record["cleanup_incomplete"] = True
                raise RuntimeError(
                    "Supervisor could not be reaped before its outer deadline"
                ) from None
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=0.5)

    def execute(self, argv: list[str], record: dict[str, Any]) -> None:
        child: subprocess.Popen[str] | None = None
        descriptor: int | None = None
        helper = Path(supervisor.__file__)
        record["supervisor_sha256"] = sha256(helper)
        report = self.output / record["supervisor_report"]
        command = [
            str(Path(sys.executable).resolve()),
            "-I",
            str(helper),
            str(report),
            str(self.timeout),
            *argv,
        ]
        try:
            with (
                (self.output / record["stdout"]).open("w") as stdout,
                (self.output / record["stderr"]).open("w") as stderr,
            ):
                with cancellation_scope() as latch, latch.deferred():
                    child = subprocess.Popen(
                        command,
                        env={
                            **self.environment,
                            supervisor.PARENT_VARIABLE: (
                                f"{os.getpid()}:{supervisor.identity(os.getpid())[1]}"
                            ),
                        },
                        cwd=self.output,
                        text=True,
                        stdout=stdout,
                        stderr=stderr,
                        start_new_session=True,
                    )
                    record["pid"] = child.pid
                    descriptor = os.pidfd_open(child.pid)
                latch.checkpoint()
                child.wait(timeout=self.timeout + supervisor.CLEANUP_SECONDS + 1)
        finally:
            with cancellation_scope() as latch, latch.deferred():
                try:
                    if child is not None:
                        self.stop(child, descriptor, record)
                        record["exit_code"] = child.returncode
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
                    if report.is_file():
                        record["supervisor"] = json.loads(report.read_text())
                    record["supervisor_unchanged"] = (
                        sha256(helper) == record["supervisor_sha256"]
                    )

    def run(self, argv: list[str]) -> str:
        cancellation.checkpoint()
        number = len(self.records)
        record: dict[str, Any] = {
            "argv": argv,
            "timeout_seconds": self.timeout,
            "exit_code": None,
            "stdout": f"command-{number:03d}.stdout",
            "stderr": f"command-{number:03d}.stderr",
            "supervisor_report": f"command-{number:03d}.supervisor.json",
        }
        self.records.append(record)
        try:
            self.execute(argv, record)
            state = record.get("supervisor", {})
            require(
                record["exit_code"] == 0
                and record.get("supervisor_unchanged") is True
                and state.get("passed") is True
                and state.get("cleanup_complete") is True
                and state.get("capabilities") == {"pidfd": True, "subreaper": True},
                "Native tool command/supervisor failed; see private logs",
            )
        except BaseException as error:
            record["error_type"] = type(error).__name__
            raise
        finally:
            with cancellation_scope() as latch, latch.deferred():
                write_json(self.output / "commands.json", self.records)
            latch.checkpoint()
        return (self.output / str(record["stdout"])).read_text()
