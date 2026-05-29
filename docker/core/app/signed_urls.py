"""HMAC-signed URL helpers for protecting resource endpoints.

Use-case: prevent hotlinking of user-specific image/memory endpoints by
issuing short-lived, signed tokens embedded in URLs. The server verifies
the signature + expiry before serving.

Secret is read from SIGNED_URL_SECRET env, falling back to VAPID_PRIVATE_KEY
(a stable secret present in every deployed environment). If neither is set in
production the signer fails closed rather than using a predictable key — see
app/secret_config.py.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from .secret_config import resolve_secret


def _secret() -> bytes:
    return resolve_secret(
        "SIGNED_URL_SECRET", "VAPID_PRIVATE_KEY", purpose="signed-urls"
    ).encode("utf-8")


def sign(resource_id: str, ttl_seconds: int = 300, user_id: str | None = None) -> str:
    """Return a URL-safe signature token `{exp}.{sig}` for resource_id.

    Includes optional user_id binding so a token for alice's image can't
    be replayed against bob's if someone captures it.

    The signature covers: "resource_id|user_id|exp"
    """
    exp = int(time.time()) + max(1, ttl_seconds)
    payload = f"{resource_id}|{user_id or ''}|{exp}".encode("utf-8")
    mac = hmac.new(_secret(), payload, hashlib.sha256).digest()
    sig = base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")
    return f"{exp}.{sig}"


def verify(token: str, resource_id: str, user_id: str | None = None) -> bool:
    """Verify a token for a resource. Returns False on any mismatch."""
    try:
        exp_part, sig_part = token.split(".", 1)
        exp = int(exp_part)
    except (ValueError, AttributeError):
        return False
    if time.time() > exp:
        return False
    payload = f"{resource_id}|{user_id or ''}|{exp}".encode("utf-8")
    expected = hmac.new(_secret(), payload, hashlib.sha256).digest()
    expected_sig = base64.urlsafe_b64encode(expected).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(expected_sig, sig_part)


def signed_path(base_path: str, resource_id: str,
                ttl_seconds: int = 300, user_id: str | None = None) -> str:
    """Convenience: append ?sig=TOKEN to an existing URL."""
    token = sign(resource_id, ttl_seconds=ttl_seconds, user_id=user_id)
    sep = "&" if "?" in base_path else "?"
    return f"{base_path}{sep}sig={token}"
