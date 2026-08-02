"""Shared fail-closed parser for the bounded non-LLM GPU lease marker."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import secrets
import time


GPU_LEASE_MARKER = Path("/run/user/1000/dominus-gpu/non-llm-lease.json")
GPU_LEASE_ACK = Path("/run/user/1000/dominus-gpu/non-llm-lease-vllm-ack.json")
GPU_LEASE_VERSION = 1
GPU_LEASE_MAX_SECONDS = 600
GPU_LEASE_WORKLOADS = frozenset({"comfyui", "companion-voice"})
GPU_LEASE_STATES = frozenset({"active", "cleaning", "cleanup_failed"})
_LEASE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class LeaseStateError(ValueError):
    """The shared lease marker is malformed and must remain fail-closed."""


@dataclass(frozen=True, slots=True)
class ActiveLease:
    lease_id: str
    workload: str
    expires_at_epoch_seconds: float
    state: str


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LeaseStateError(f"lease {field} is not numeric")
    result = float(value)
    if not math.isfinite(result):
        raise LeaseStateError(f"lease {field} is not finite")
    return result


def _read_document(path: Path) -> object | None:
    if path.is_symlink():
        raise LeaseStateError("lease marker must not be a symlink")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LeaseStateError(f"lease marker is unreadable: {error}") from error


def _validate(document: object, now: float) -> ActiveLease:
    if not isinstance(document, dict):
        raise LeaseStateError("lease marker is not an object")
    if document.get("version") != GPU_LEASE_VERSION:
        raise LeaseStateError("lease marker version is unsupported")
    lease_id = document.get("lease_id")
    token_sha256 = document.get("token_sha256")
    workload = document.get("workload")
    state = document.get("state", "active")
    ttl_seconds = document.get("ttl_seconds")
    if not isinstance(lease_id, str) or _LEASE_ID_PATTERN.fullmatch(lease_id) is None:
        raise LeaseStateError("lease_id is invalid")
    if (
        not isinstance(token_sha256, str)
        or _SHA256_PATTERN.fullmatch(token_sha256) is None
    ):
        raise LeaseStateError("lease token digest is invalid")
    if not isinstance(workload, str) or workload not in GPU_LEASE_WORKLOADS:
        raise LeaseStateError("lease workload is invalid")
    if not isinstance(state, str) or state not in GPU_LEASE_STATES:
        raise LeaseStateError("lease state is invalid")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise LeaseStateError("lease TTL is invalid")
    if not 1 <= ttl_seconds <= GPU_LEASE_MAX_SECONDS:
        raise LeaseStateError("lease exceeds the 600-second ceiling")
    issued_at = _number(document.get("issued_at_epoch_seconds"), "issued_at")
    expires_at = _number(document.get("expires_at_epoch_seconds"), "expires_at")
    if issued_at > now + 5:
        raise LeaseStateError("lease claims a future issue time")
    if abs(expires_at - (issued_at + ttl_seconds)) > 0.001:
        raise LeaseStateError("lease expiry does not match its bounded TTL")
    return ActiveLease(
        lease_id=lease_id,
        workload=workload,
        expires_at_epoch_seconds=expires_at,
        state=state,
    )


def _remove_ack(ack_path: Path) -> None:
    try:
        ack_path.unlink()
    except FileNotFoundError:
        pass


def active_lease(
    marker_path: Path = GPU_LEASE_MARKER,
    ack_path: Path = GPU_LEASE_ACK,
    now: float | None = None,
) -> ActiveLease | None:
    current = time.time() if now is None else now
    document = _read_document(marker_path)
    if document is None:
        return None
    # Expiry is a cleanup deadline, not permission to reopen the GPU. The
    # gateway retains active/cleaning/cleanup_failed markers until its
    # workload-specific cleaner proves quiescence. Native vLLM must therefore
    # block and acknowledge every valid marker, including an expired one.
    return _validate(document, current)


def clear_orphan_ack(
    marker_path: Path = GPU_LEASE_MARKER, ack_path: Path = GPU_LEASE_ACK
) -> None:
    if not marker_path.exists():
        _remove_ack(ack_path)


def acknowledged(lease_id: str, ack_path: Path = GPU_LEASE_ACK) -> bool:
    if ack_path.is_symlink():
        raise LeaseStateError("lease acknowledgement must not be a symlink")
    try:
        document = json.loads(ack_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LeaseStateError(f"lease acknowledgement is unreadable: {error}") from error
    return (
        isinstance(document, dict)
        and document.get("version") == GPU_LEASE_VERSION
        and document.get("lease_id") == lease_id
    )


def acknowledge(
    lease_id: str,
    ack_path: Path = GPU_LEASE_ACK,
    now: float | None = None,
) -> None:
    if _LEASE_ID_PATTERN.fullmatch(lease_id) is None:
        raise LeaseStateError("cannot acknowledge an invalid lease_id")
    temporary = ack_path.with_name(
        f".{ack_path.name}.{os.getpid()}.{secrets.token_hex(8)}"
    )
    payload = json.dumps(
        {
            "version": GPU_LEASE_VERSION,
            "lease_id": lease_id,
            "acknowledged_at_epoch_seconds": time.time() if now is None else now,
        },
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, ack_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
