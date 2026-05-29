"""Centralised secret resolution — never a predictable fallback.

A missing signing secret must NEVER fall back to a value baked into the source.
The old ``"dev-secret"`` / ``"dev-audit-chain-secret"`` fallbacks meant a
deployment that forgot to set the env var would sign URLs and compute
audit-chain hashes with a value anyone could read in the repo — making signed
URLs forgeable and defeating the audit chain's tamper-evidence.

Resolution rules:
  * Return the first env var (in priority order) that is actually set.
  * If none are set:
      - in an explicitly non-production context (pytest, or
        ``KLUKAI_ALLOW_DEV_SECRETS=1``), return a deterministic placeholder so
        the suite / local dev can sign+verify;
      - otherwise generate a STRONG random secret once per process. It is never
        the guessable literal and never crashes the service; the only tradeoff
        is that signed URLs / audit-chain continuity reset on restart, so set a
        real env secret for cross-restart or multi-instance stability.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys

logger = logging.getLogger(__name__)

# Per-process cache of generated secrets (keyed by purpose) so sign/verify
# round-trips within one process when no env secret is configured.
_GENERATED: dict[str, str] = {}


def dev_secrets_allowed() -> bool:
    """True only in an explicitly non-production context.

    Production (uvicorn) never imports pytest and does not set the override, so
    a missing secret there raises rather than using a placeholder.
    """
    if os.environ.get("KLUKAI_ALLOW_DEV_SECRETS") == "1":
        return True
    return "pytest" in sys.modules


def resolve_secret(*env_names: str, purpose: str) -> str:
    """Return the first set env var among *env_names*, else a safe fallback.

    Reads the environment on every call (no caching of env values) so tests can
    monkeypatch. When nothing is configured it never returns a predictable
    literal: a deterministic placeholder under pytest/dev, otherwise a strong
    random secret generated once per process.
    """
    for name in env_names:
        value = os.environ.get(name)
        if value:
            return value

    if dev_secrets_allowed():
        logger.warning(
            "No secret configured for %s (tried: %s). Using a deterministic dev "
            "placeholder — set one of these env vars before production use.",
            purpose,
            ", ".join(env_names),
        )
        return f"dev-insecure-{purpose}"

    # Production with nothing configured: generate a strong random secret once
    # per process. Never the guessable literal, never a crash. Stable for this
    # process so sign/verify round-trips; resets on restart (set an env var for
    # cross-restart / multi-instance stability).
    if purpose not in _GENERATED:
        _GENERATED[purpose] = secrets.token_urlsafe(48)
        logger.warning(
            "No secret configured for %s (tried: %s). Generated a strong "
            "ephemeral per-process secret; signed-URL / audit-chain continuity "
            "resets on restart. Set one of these env vars for stability.",
            purpose,
            ", ".join(env_names),
        )
    return _GENERATED[purpose]
