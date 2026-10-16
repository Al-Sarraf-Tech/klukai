"""Fail-closed validation of the gateway-owned XTTS GPU capability."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException

GPU_LEASE_VERSION = 1
GPU_LEASE_MAX_SECONDS = 600
GPU_LEASE_WORKLOAD = "companion-voice"
_LEASE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _lease_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("lease timestamp is not numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("lease timestamp is not finite")
    return result


def _validate_document(document: dict[str, Any], current: float) -> str:
    if document.get("version") != GPU_LEASE_VERSION:
        raise ValueError("lease version is unsupported")
    lease_id = document.get("lease_id")
    digest = document.get("token_sha256")
    ttl_seconds = document.get("ttl_seconds")
    if not isinstance(lease_id, str) or _LEASE_ID_PATTERN.fullmatch(lease_id) is None:
        raise ValueError("lease id is invalid")
    if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
        raise ValueError("lease token digest is invalid")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 1 <= ttl_seconds <= GPU_LEASE_MAX_SECONDS
    ):
        raise ValueError("lease TTL is invalid")
    issued_at = _lease_number(document.get("issued_at_epoch_seconds"))
    expires_at = _lease_number(document.get("expires_at_epoch_seconds"))
    if issued_at > current + 5:
        raise ValueError("lease issue time is in the future")
    if abs(expires_at - (issued_at + ttl_seconds)) > 0.001:
        raise ValueError("lease expiry does not match its TTL")
    if document.get("workload") != GPU_LEASE_WORKLOAD:
        raise HTTPException(
            status_code=403, detail="GPU lease belongs to another workload"
        )
    if document.get("state", "active") != "active" or expires_at <= current:
        raise HTTPException(
            status_code=503, detail="companion voice GPU lease is not active"
        )
    return digest


def require_xtts_gpu_lease(
    token: str | None, marker: Path, now: float | None = None
) -> None:
    """Validate one opaque capability without owning or mutating its marker."""
    if token is None or not token or len(token) > 256:
        raise HTTPException(status_code=403, detail="matching GPU lease token required")
    if marker.is_symlink():
        raise HTTPException(status_code=503, detail="GPU lease state is invalid")
    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503, detail="active companion voice GPU lease required"
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=503, detail="GPU lease state is invalid"
        ) from exc
    current = time.time() if now is None else now
    try:
        if not isinstance(document, dict):
            raise ValueError("lease marker is not an object")
        digest = _validate_document(document, current)
    except HTTPException:
        raise
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=503, detail="GPU lease state is invalid"
        ) from exc
    supplied = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(supplied, digest):
        raise HTTPException(status_code=403, detail="GPU lease token is invalid")
