"""Request peer identity and explicit internal voice authentication headers."""

from __future__ import annotations
import os
from fastapi import Request


def voice_auth_headers() -> dict[str, str]:
    """Bearer header for calls to the voice service when VOICE_API_TOKEN is set.

    Empty (no auth) when unset, so it stays a no-op until the token is
    provisioned on the voice host — core->voice keeps working either way.
    """
    tok = os.environ.get("VOICE_API_TOKEN")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def client_ip(request: "Request") -> str:
    """Best-effort real client IP behind Cloudflare → cloudflared → loopback.

    The socket peer (request.client.host) is always the tunnel's internal IP, so
    a naive per-IP ban or audit would treat every external user as one host —
    locking out the owner and recording useless forensics. Prefer Cloudflare's
    CF-Connecting-IP, then the first X-Forwarded-For hop, then the peer.
    """
    cf = request.headers.get("cf-connecting-ip")
    if cf and cf.strip():
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff and xff.strip():
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
