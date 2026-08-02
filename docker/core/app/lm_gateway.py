"""Shared policy for authenticated requests to the local LM gateway."""

from __future__ import annotations

import os


# This is a hard residency ceiling, not a deployment tuning knob.
LM_TTL_SECONDS = 15 * 60


def lm_studio_auth_headers() -> dict[str, str]:
    """Return the required bearer header without retaining or logging the token."""
    token = os.environ.get("LM_STUDIO_TOKEN")
    if token is None or not token.strip():
        raise RuntimeError("LM_STUDIO_TOKEN is required for local LM gateway requests")
    return {"Authorization": f"Bearer {token}"}
