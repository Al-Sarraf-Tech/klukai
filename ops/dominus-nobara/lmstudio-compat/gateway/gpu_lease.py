"""Restart-safe, bounded non-LLM GPU lease state.

The opaque release token is returned to the authenticated acquirer exactly
once. Only its SHA-256 digest is persisted in the shared runtime marker.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any


LEASE_VERSION = 1
LEASE_MAX_SECONDS = 600
LEASE_DEFAULT_SECONDS = 600
SUPPORTED_WORKLOADS = frozenset({"comfyui", "companion-voice"})
_LEASE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class LeaseStateError(ValueError):
    """The persistent lease marker cannot be trusted."""


class LeaseBusyError(RuntimeError):
    """A valid non-expired lease already owns the GPU."""


class LeaseTokenError(PermissionError):
    """A release token does not own the active lease."""


@dataclass(frozen=True, slots=True)
class Lease:
    lease_id: str
    token_sha256: str
    workload: str
    issued_at_epoch_seconds: float
    expires_at_epoch_seconds: float
    ttl_seconds: int
    state: str

    def remaining_seconds(self, now: float | None = None) -> int:
        current = time.time() if now is None else now
        return max(0, math.ceil(self.expires_at_epoch_seconds - current))

    def is_expired(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return self.expires_at_epoch_seconds <= current


@dataclass(frozen=True, slots=True)
class AcquiredLease:
    lease: Lease
    token: str


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LeaseStateError(f"lease marker {field} is not numeric")
    result = float(value)
    if not math.isfinite(result):
        raise LeaseStateError(f"lease marker {field} is not finite")
    return result


class LeaseStore:
    """Atomic marker operations for one single-worker gateway process."""

    def __init__(self, marker_path: Path, ack_path: Path) -> None:
        self.marker_path = marker_path
        self.ack_path = ack_path
        if marker_path.parent != ack_path.parent:
            raise LeaseStateError("lease marker and acknowledgement must share a directory")
        if not marker_path.parent.is_dir():
            raise LeaseStateError(
                f"GPU lease runtime directory is absent: {marker_path.parent}"
            )

    def _atomic_document(self, path: Path, document: dict[str, object]) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}")
        payload = json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                # The companion-voice process joins the gateway's runtime
                # group and validates the token digest directly before XTTS
                # can load. The opaque capability itself is never persisted.
                os.chmod(temporary, 0o640)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _read_marker(self) -> object:
        if self.marker_path.is_symlink():
            raise LeaseStateError("GPU lease marker must not be a symlink")
        try:
            return json.loads(self.marker_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LeaseStateError(f"GPU lease marker is unreadable: {error}") from error

    def _validated(self, document: object, now: float) -> Lease:
        if not isinstance(document, dict):
            raise LeaseStateError("GPU lease marker is not an object")
        if document.get("version") != LEASE_VERSION:
            raise LeaseStateError("GPU lease marker version is unsupported")
        lease_id = document.get("lease_id")
        token_sha256 = document.get("token_sha256")
        workload = document.get("workload")
        state = document.get("state", "active")
        ttl_seconds = document.get("ttl_seconds")
        if not isinstance(lease_id, str) or _LEASE_ID_PATTERN.fullmatch(lease_id) is None:
            raise LeaseStateError("GPU lease marker lease_id is invalid")
        if (
            not isinstance(token_sha256, str)
            or _SHA256_PATTERN.fullmatch(token_sha256) is None
        ):
            raise LeaseStateError("GPU lease marker token digest is invalid")
        if not isinstance(workload, str) or workload not in SUPPORTED_WORKLOADS:
            raise LeaseStateError("GPU lease marker workload is invalid")
        if state not in {"active", "cleaning", "cleanup_failed"}:
            raise LeaseStateError("GPU lease marker state is invalid")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise LeaseStateError("GPU lease marker ttl_seconds is invalid")
        if not 1 <= ttl_seconds <= LEASE_MAX_SECONDS:
            raise LeaseStateError("GPU lease marker exceeds the 600-second ceiling")
        issued_at = _number(document.get("issued_at_epoch_seconds"), "issued_at")
        expires_at = _number(document.get("expires_at_epoch_seconds"), "expires_at")
        if issued_at > now + 5:
            raise LeaseStateError("GPU lease marker claims a future issue time")
        if abs(expires_at - (issued_at + ttl_seconds)) > 0.001:
            raise LeaseStateError("GPU lease marker expiry does not match its bounded TTL")
        return Lease(
            lease_id=lease_id,
            token_sha256=token_sha256,
            workload=workload,
            issued_at_epoch_seconds=issued_at,
            expires_at_epoch_seconds=expires_at,
            ttl_seconds=ttl_seconds,
            state=state,
        )

    def active(self, now: float | None = None) -> Lease | None:
        """Return any marker, including expired/dirty state, until safe cleanup."""

        current = time.time() if now is None else now
        document = self._read_marker()
        if document is None:
            return None
        return self._validated(document, current)

    def acquire(self, workload: str, requested_ttl_seconds: int) -> AcquiredLease:
        if workload not in SUPPORTED_WORKLOADS:
            raise ValueError("workload does not have a fail-closed GPU cleaner")
        if (
            isinstance(requested_ttl_seconds, bool)
            or not isinstance(requested_ttl_seconds, int)
            or requested_ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be a positive integer")
        existing = self.active()
        if existing is not None:
            raise LeaseBusyError("the GPU already has an active non-LLM lease")

        ttl_seconds = min(requested_ttl_seconds, LEASE_MAX_SECONDS)
        token = secrets.token_urlsafe(32)
        now = time.time()
        lease = Lease(
            lease_id=secrets.token_hex(16),
            token_sha256=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            workload=workload,
            issued_at_epoch_seconds=now,
            expires_at_epoch_seconds=now + ttl_seconds,
            ttl_seconds=ttl_seconds,
            state="active",
        )
        self._remove_ack()
        self._atomic_document(
            self.marker_path,
            {
                "version": LEASE_VERSION,
                "lease_id": lease.lease_id,
                "token_sha256": lease.token_sha256,
                "workload": lease.workload,
                "issued_at_epoch_seconds": lease.issued_at_epoch_seconds,
                "expires_at_epoch_seconds": lease.expires_at_epoch_seconds,
                "ttl_seconds": lease.ttl_seconds,
                "state": lease.state,
            },
        )
        return AcquiredLease(lease=lease, token=token)

    def owned_by(self, token: str) -> Lease | None:
        """Return the retained lease marker only when the opaque token owns it.

        Expired and cleanup-failed leases remain addressable by their owner so
        an explicit release can retry cleanup. This method never removes the
        marker; only ``complete_cleanup`` may do that.
        """

        lease = self.active()
        if lease is None:
            return None
        supplied = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not secrets.compare_digest(supplied, lease.token_sha256):
            raise LeaseTokenError("token does not own the active GPU lease")
        return lease

    def abort(self, lease_id: str) -> None:
        """Remove only the lease created by the failing acquisition attempt."""

        self._remove_if_lease_id(lease_id)

    def complete_cleanup(self, lease_id: str) -> None:
        """Remove a lease only after its workload cleaner proved quiescence."""

        self._remove_if_lease_id(lease_id)

    def mark_state(self, lease_id: str, state: str) -> None:
        if state not in {"cleaning", "cleanup_failed"}:
            raise ValueError("unsupported GPU lease transition")
        document = self._read_marker()
        if not isinstance(document, dict) or document.get("lease_id") != lease_id:
            raise LeaseStateError("cannot update a missing or different GPU lease")
        # Validate every immutable field before carrying it into a new marker.
        self._validated(document, time.time())
        updated = dict(document)
        updated["state"] = state
        self._atomic_document(self.marker_path, updated)

    def _remove_if_lease_id(self, lease_id: str) -> None:
        document = self._read_marker()
        if document is None:
            self._remove_ack()
            return
        if not isinstance(document, dict) or document.get("lease_id") != lease_id:
            raise LeaseStateError("refusing to remove a different GPU lease")
        self.marker_path.unlink()
        self._remove_ack()

    def _remove_ack(self) -> None:
        try:
            self.ack_path.unlink()
        except FileNotFoundError:
            pass

    def acknowledged(self, lease_id: str) -> bool:
        if self.ack_path.is_symlink():
            raise LeaseStateError("native vLLM acknowledgement must not be a symlink")
        try:
            document = json.loads(self.ack_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LeaseStateError(
                f"native vLLM acknowledgement is unreadable: {error}"
            ) from error
        return (
            isinstance(document, dict)
            and document.get("version") == LEASE_VERSION
            and document.get("lease_id") == lease_id
        )
