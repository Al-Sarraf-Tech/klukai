#!/usr/bin/env python3
"""Independent fail-closed hard-stop watchdog for native vLLM."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from pathlib import PurePosixPath
import subprocess
import time

from dominus_gpu_lease import (
    LeaseStateError,
    acknowledge,
    acknowledged,
    active_lease,
    clear_orphan_ack,
)


BACKEND_UNIT = "vllm-server.service"
RAID_MOUNT = Path("/mnt/nvmer0")
STATE_PATH = RAID_MOUNT / "services/ai-stack/state/vllm/activity-state.json"
LOG_PATH = RAID_MOUNT / "services/ai-stack/logs/vllm/vllm-idle-watchdog.log"
GAME_MARKER = Path("/run/user/1000/dominus-gpu/game-active")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
MAX_IDLE_TTL_SECONDS = 900
HARD_STOP_AFTER_NS = 895_000_000_000
POLL_SECONDS = 0.25
BACKEND_QUIESCED = "quiesced"
BACKEND_RUNNING = "running"
BACKEND_UNSAFE = "unsafe"

BOOT_ID = BOOT_ID_PATH.read_text(encoding="utf-8").strip()
log = logging.getLogger("dominus-vllm-idle-watchdog")


def configure_logging() -> None:
    handler: logging.Handler
    if os.path.ismount(RAID_MOUNT) and LOG_PATH.parent.is_dir():
        handler = RotatingFileHandler(
            LOG_PATH, maxBytes=25 * 1024 * 1024, backupCount=4, encoding="utf-8"
        )
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [dominus-vllm-watchdog] %(levelname)s %(message)s")
    )
    log.setLevel(logging.INFO)
    log.addHandler(handler)


def systemctl(*arguments: str, timeout: float = 8.0) -> int:
    try:
        return subprocess.run(
            ["systemctl", "--user", *arguments],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        ).returncode
    except subprocess.TimeoutExpired:
        return 124


def hard_stop(reason: str) -> bool:
    log.warning("hard-stopping %s: %s", BACKEND_UNIT, reason)
    kill_result = systemctl(
        "kill", "--kill-whom=all", "--signal=SIGKILL", BACKEND_UNIT, timeout=3
    )
    stop_result = systemctl("stop", BACKEND_UNIT, timeout=8)
    if kill_result != 0 or stop_result != 0:
        log.error(
            "systemd kill/stop failed for %s (kill=%d stop=%d)",
            BACKEND_UNIT,
            kill_result,
            stop_result,
        )
        return False
    return True


def backend_evidence(
    cgroup_root: Path = Path("/sys/fs/cgroup"), timeout: float = 3.0
) -> str:
    """Classify positive systemd/cgroup evidence without guessing on errors."""

    try:
        process = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                BACKEND_UNIT,
                "--property=LoadState",
                "--property=ActiveState",
                "--property=MainPID",
                "--property=ControlGroup",
                "--no-pager",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return BACKEND_UNSAFE
    if process.returncode != 0:
        return BACKEND_UNSAFE
    properties: dict[str, str] = {}
    for line in process.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in properties:
            return BACKEND_UNSAFE
        properties[key] = value
    if set(properties) != {"LoadState", "ActiveState", "MainPID", "ControlGroup"}:
        return BACKEND_UNSAFE
    if properties["LoadState"] != "loaded":
        return BACKEND_UNSAFE
    active_state = properties["ActiveState"]
    if active_state not in {"active", "inactive", "failed"}:
        return BACKEND_UNSAFE
    if active_state == "active" or properties["MainPID"] != "0":
        return BACKEND_RUNNING

    control_group_value = properties["ControlGroup"]
    if not control_group_value:
        # systemd removes an inactive service's cgroup and reports an empty
        # ControlGroup. LoadState/ActiveState/MainPID above are still required.
        return BACKEND_QUIESCED
    control_group = PurePosixPath(control_group_value)
    if str(control_group) in {".", "/"}:
        return BACKEND_UNSAFE
    if not control_group.is_absolute() or ".." in control_group.parts:
        return BACKEND_UNSAFE
    group_path = cgroup_root.joinpath(*control_group.parts[1:])
    if not group_path.exists():
        return BACKEND_QUIESCED
    if not group_path.is_dir():
        return BACKEND_UNSAFE
    try:
        for process_file in group_path.rglob("cgroup.procs"):
            if process_file.is_file() and process_file.read_text(encoding="utf-8").strip():
                return BACKEND_RUNNING
    except OSError:
        return BACKEND_UNSAFE
    return BACKEND_QUIESCED


def backend_quiesced(
    cgroup_root: Path = Path("/sys/fs/cgroup"), timeout: float = 3.0
) -> bool:
    """Return true only with positive systemd and cgroup quiescence evidence."""

    return backend_evidence(cgroup_root, timeout) == BACKEND_QUIESCED


def prepare_backend_for_lease(reason: str) -> bool:
    """Idempotently stop vLLM, but never bless unknown/transitional evidence."""

    evidence = backend_evidence()
    if evidence == BACKEND_QUIESCED:
        return True
    stopped = hard_stop(reason)
    if evidence == BACKEND_UNSAFE or not stopped:
        return False
    return backend_quiesced()


def validate_document(document: object, now_ns: int, boot_id: str = BOOT_ID) -> int:
    if not isinstance(document, dict):
        raise ValueError("activity state is not an object")
    if document.get("version") != 1:
        raise ValueError("unsupported activity state version")
    if document.get("boot_id") != boot_id:
        raise ValueError("activity state belongs to another boot")
    if document.get("max_idle_ttl_seconds") != MAX_IDLE_TTL_SECONDS:
        raise ValueError("activity state does not contain the hard 900-second ceiling")
    last_activity = document.get("last_activity_monotonic_ns")
    deadline = document.get("hard_stop_monotonic_ns")
    if not isinstance(last_activity, int) or not isinstance(deadline, int):
        raise ValueError("activity state has no armed deadline")
    if last_activity > now_ns:
        raise ValueError("activity state claims a future monotonic timestamp")
    if deadline != last_activity + HARD_STOP_AFTER_NS:
        raise ValueError("activity deadline is not the locked 895-second safety boundary")
    return deadline


def locked_deadline(
    state_path: Path = STATE_PATH, now_ns: int | None = None, boot_id: str = BOOT_ID
) -> int:
    document = json.loads(state_path.read_text(encoding="utf-8"))
    return validate_document(
        document,
        time.monotonic_ns() if now_ns is None else now_ns,
        boot_id,
    )


def enforce_idle_deadline() -> float:
    """Run one fail-closed idle-policy iteration and return the next delay."""

    evidence = backend_evidence()
    if evidence == BACKEND_QUIESCED:
        return POLL_SECONDS
    if evidence == BACKEND_UNSAFE:
        hard_stop("cannot obtain reliable systemd/cgroup activity evidence")
        return POLL_SECONDS
    if not os.path.ismount(RAID_MOUNT):
        hard_stop("required RAID mount disappeared")
        return POLL_SECONDS
    try:
        deadline = locked_deadline()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        hard_stop(f"missing or malformed fail-closed deadline: {error}")
        return POLL_SECONDS
    remaining_ns = deadline - time.monotonic_ns()
    if remaining_ns <= 0:
        hard_stop("895-second idle safety deadline reached")
        return POLL_SECONDS
    return min(POLL_SECONDS, remaining_ns / 1_000_000_000)


def main() -> int:
    configure_logging()
    log.info("watchdog active; 250 ms backstop, hard stop at 895 idle seconds")
    while True:
        try:
            gpu_lease = active_lease()
        except (OSError, LeaseStateError) as error:
            prepare_backend_for_lease(f"invalid non-LLM GPU lease marker: {error}")
            time.sleep(POLL_SECONDS)
            continue
        if gpu_lease is not None:
            stopped = prepare_backend_for_lease(
                f"GPU leased to {gpu_lease.workload}"
            )
            if stopped:
                # Re-read after the stop: never acknowledge a lease that was
                # released or replaced while systemd was quiescing.
                try:
                    current_lease = active_lease()
                    if (
                        current_lease is not None
                        and current_lease.lease_id == gpu_lease.lease_id
                        and backend_quiesced()
                        and not acknowledged(gpu_lease.lease_id)
                    ):
                        acknowledge(gpu_lease.lease_id)
                except (OSError, LeaseStateError) as error:
                    log.error("cannot acknowledge native-vLLM quiescence: %s", error)
            time.sleep(POLL_SECONDS)
            continue
        try:
            clear_orphan_ack()
        except OSError as error:
            log.error("cannot clear orphaned GPU-lease acknowledgement: %s", error)
        if GAME_MARKER.exists():
            prepare_backend_for_lease("GameMode marker is active")
            time.sleep(POLL_SECONDS)
            continue
        time.sleep(enforce_idle_deadline())


if __name__ == "__main__":
    raise SystemExit(main())
