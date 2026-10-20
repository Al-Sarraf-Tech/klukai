"""Shared policy for authenticated requests to the local LM gateway."""

from __future__ import annotations

import os

import httpx


# This is a hard residency ceiling, not a deployment tuning knob.
LM_TTL_SECONDS = 15 * 60

# Dominus is routinely powered off. Over Tailscale an offline peer never
# answers the SYN, so without a short connect phase every local request waited
# out its whole timeout (120 s) holding the shared LM gate, and background
# steps queued behind it for minutes. A live gateway connects in milliseconds;
# model loading happens after connect and is still covered by the read timeout.
LM_CONNECT_TIMEOUT_SECONDS = 5.0


def lm_timeout(total: float) -> httpx.Timeout:
    """``total`` for read/write/pool, but a short connect to the LM gateway."""
    return httpx.Timeout(total, connect=LM_CONNECT_TIMEOUT_SECONDS)


def lm_studio_auth_headers() -> dict[str, str]:
    """Return the required bearer header without retaining or logging the token."""
    token = os.environ.get("LM_STUDIO_TOKEN")
    if token is None or not token.strip():
        raise RuntimeError("LM_STUDIO_TOKEN is required for local LM gateway requests")
    return {"Authorization": f"Bearer {token}"}
