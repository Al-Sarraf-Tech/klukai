"""Audit log tamper-detection via HMAC hash chain.

Each audit event is hashed together with the hash of the previous event,
forming a chain. Deleting or modifying any row breaks the chain, which
a periodic verifier can detect.

NOTE: this module provides helpers. Actual chain writes happen in
audit.log via a small additive layer.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _chain_secret() -> bytes:
    return (
        os.environ.get("AUDIT_CHAIN_SECRET")
        or os.environ.get("SIGNED_URL_SECRET")
        or "dev-audit-chain-secret"
    ).encode("utf-8")


def compute_row_hash(
    row_id: int,
    event_type: str,
    user_id: str | None,
    ip_address: str | None,
    request_id: str | None,
    metadata: dict | None,
    created_at: str,
    prev_hash: str | None,
) -> str:
    """Compute HMAC-SHA256 over canonical row representation + prev_hash."""
    payload = json.dumps({
        "id": row_id,
        "event_type": event_type,
        "user_id": user_id,
        "ip_address": ip_address,
        "request_id": request_id,
        "metadata": metadata,
        "created_at": created_at,
        "prev": prev_hash or "",
    }, sort_keys=True, default=str)
    return hmac.new(_chain_secret(), payload.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def verify_chain(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Re-compute the hash chain over an ordered row list and report breaks.

    Rows should be oldest-first. Each dict must contain all fields used
    by compute_row_hash PLUS a `chain_hash` field holding the previously-
    stored hash.

    Returns: {"valid": bool, "break_at_id": int | None,
              "checked": int, "expected_hash": str | None}.
    """
    prev_hash: str | None = None
    for r in rows:
        computed = compute_row_hash(
            row_id=r["id"],
            event_type=r["event_type"],
            user_id=r.get("user_id"),
            ip_address=r.get("ip_address"),
            request_id=r.get("request_id"),
            metadata=r.get("metadata"),
            created_at=str(r.get("created_at") or ""),
            prev_hash=prev_hash,
        )
        stored = r.get("chain_hash")
        if stored and stored != computed:
            return {
                "valid": False,
                "break_at_id": r["id"],
                "checked": rows.index(r),
                "expected_hash": computed,
            }
        prev_hash = stored or computed
    return {
        "valid": True,
        "break_at_id": None,
        "checked": len(rows),
        "expected_hash": prev_hash,
    }
