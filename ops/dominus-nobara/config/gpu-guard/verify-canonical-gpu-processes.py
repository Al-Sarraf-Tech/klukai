#!/usr/bin/env python3
"""Detect residual Dominus AI compute processes without killing unknown work."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import subprocess
import sys


CANONICAL_COMMAND = re.compile(
    r"(?:llama-server|llama\.cpp|ComfyUI|speaches|TranscriptionSuite|"
    r"\bvllm\b|\bxtts\b|coqui|dominus[-_ ].*voice)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ComputeProcess:
    pid: int
    process_name: str
    used_gpu_memory_mib: str
    command_line: str
    cgroup: str


def is_canonical(
    process_name: str,
    command_line: str,
    cgroup: str,
    container_ids: tuple[str, ...],
) -> bool:
    evidence = f"{process_name} {command_line}"
    if CANONICAL_COMMAND.search(evidence) is not None:
        return True
    return any(container_id and container_id in cgroup for container_id in container_ids)


def parse_compute_csv(output: str) -> list[tuple[int, str, str]]:
    rows: list[tuple[int, str, str]] = []
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        columns = [column.strip() for column in raw_line.split(",", 2)]
        if len(columns) != 3 or not columns[0].isdigit():
            raise ValueError(f"unparseable nvidia-smi compute row: {raw_line!r}")
        rows.append((int(columns[0]), columns[1], columns[2]))
    return rows


def process_details(pid: int, proc_root: Path = Path("/proc")) -> tuple[str, str]:
    process_root = proc_root / str(pid)
    try:
        command_line = (
            (process_root / "cmdline")
            .read_bytes()
            .replace(b"\0", b" ")
            .decode("utf-8", errors="replace")
            .strip()
        )
        cgroup = (process_root / "cgroup").read_text(
            encoding="utf-8", errors="replace"
        )
    except FileNotFoundError:
        return "", ""
    return command_line, cgroup


def canonical_processes(
    csv_output: str,
    container_ids: tuple[str, ...],
    proc_root: Path = Path("/proc"),
) -> list[ComputeProcess]:
    result: list[ComputeProcess] = []
    for pid, process_name, memory in parse_compute_csv(csv_output):
        command_line, cgroup = process_details(pid, proc_root)
        if not command_line and not cgroup:
            continue
        if is_canonical(process_name, command_line, cgroup, container_ids):
            result.append(
                ComputeProcess(pid, process_name, memory, command_line, cgroup.strip())
            )
    return result


def nvidia_compute_csv() -> str:
    process = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if process.returncode != 0:
        message = process.stderr.strip() or f"nvidia-smi exited {process.returncode}"
        raise RuntimeError(message)
    return process.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container-id", action="append", default=[])
    arguments = parser.parse_args()
    try:
        residual = canonical_processes(
            nvidia_compute_csv(), tuple(arguments.container_id)
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as error:
        print(json.dumps({"status": "unverifiable", "error": str(error)}))
        return 2
    if residual:
        print(
            json.dumps(
                {
                    "status": "canonical_gpu_processes_remain",
                    "processes": [asdict(process) for process in residual],
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps({"status": "quiesced", "canonical_compute_processes": 0}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
