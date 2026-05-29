"""Centralised secret resolution — fail-closed in production.

A missing signing secret must NEVER silently fall back to a predictable
literal. The old ``"dev-secret"`` / ``"dev-audit-chain-secret"`` fallbacks meant
that a deployment which forgot to set the env var would happily sign URLs and
compute audit-chain hashes with a value anyone could read in the source —
making signed URLs forgeable and defeating the audit chain's tamper-evidence.

Resolution rules:
  * Return the first env var (in priority order) that is actually set.
  * If none are set:
      - in an explicitly non-production context (running under pytest, or
        ``KLUKAI_ALLOW_DEV_SECRETS=1``), return a deterministic placeholder so
        the test suite / local dev can run, and log a loud warning;
      - otherwise raise ``RuntimeError`` — a production process with no signing
        secret should fail loudly, not sign with a guessable key.
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)


def dev_secrets_allowed() -> bool:
    """True only in an explicitly non-production context.

    Production (uvicorn) never imports pytest and does not set the override, so
    a missing secret there raises rather than using a placeholder.
    """
    if os.environ.get("KLUKAI_ALLOW_DEV_SECRETS") == "1":
        return True
    return "pytest" in sys.modules


def resolve_secret(*env_names: str, purpose: str) -> str:
    """Return the first set env var among *env_names*.

    Raises ``RuntimeError`` if none are set and dev secrets are not allowed.
    Reads the environment on every call (no caching) so tests can monkeypatch.
    """
    for name in env_names:
        value = os.environ.get(name)
        if value:
            return value

    if dev_secrets_allowed():
        logger.warning(
            "No secret configured for %s (tried: %s). Using an INSECURE dev "
            "placeholder — set one of these env vars before production use.",
            purpose,
            ", ".join(env_names),
        )
        return f"dev-insecure-{purpose}"

    raise RuntimeError(
        f"No secret configured for {purpose}. Set one of: {', '.join(env_names)}. "
        "Refusing to fall back to a predictable value. "
        "(Set KLUKAI_ALLOW_DEV_SECRETS=1 only for local/dev/test.)"
    )
