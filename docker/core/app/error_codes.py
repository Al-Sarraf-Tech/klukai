"""Canonical error code catalog + JSON response helpers.

Every error response that clients may branch on should use `err()` to
emit a consistent `{error, code}` shape so the UI can switch on `code`
without parsing the human message.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse


# ── Catalog ─────────────────────────────────────────────────────────────────
# Format: CATEGORY_REASON. Keep namespaces stable; new codes append only.

# Auth
AUTH_REQUIRED       = "AUTH_REQUIRED"
AUTH_INVALID        = "AUTH_INVALID"
AUTH_BANNED         = "AUTH_BANNED"
AUTH_EXPIRED        = "AUTH_EXPIRED"
ADMIN_ONLY          = "ADMIN_ONLY"

# Input validation
INPUT_INVALID       = "INPUT_INVALID"
INPUT_TOO_SHORT     = "INPUT_TOO_SHORT"
INPUT_TOO_LONG      = "INPUT_TOO_LONG"
INPUT_MISSING       = "INPUT_MISSING"

# Rate limiting
RATE_LIMITED        = "RATE_LIMITED"

# Resource state
NOT_FOUND           = "NOT_FOUND"
ALREADY_EXISTS      = "ALREADY_EXISTS"
CONFLICT            = "CONFLICT"

# Subsystem failures
DB_UNAVAILABLE      = "DB_UNAVAILABLE"
REDIS_UNAVAILABLE   = "REDIS_UNAVAILABLE"
VOICE_UNAVAILABLE   = "VOICE_UNAVAILABLE"
LLM_UNAVAILABLE     = "LLM_UNAVAILABLE"
IMAGE_GEN_FAILED    = "IMAGE_GEN_FAILED"

# Generic
INTERNAL_ERROR      = "INTERNAL_ERROR"
FEATURE_DISABLED    = "FEATURE_DISABLED"


def err(code: str, message: str, status_code: int = 400,
        extra: dict | None = None) -> JSONResponse:
    """Build a structured error response.

    Response shape: {"error": message, "code": code, **extra}

    Use this for all new endpoints. Existing `JSONResponse({"error": ...})`
    calls may migrate opportunistically.
    """
    body: dict = {"error": message, "code": code}
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status_code)


def auth_required() -> JSONResponse:
    return err(AUTH_REQUIRED, "Authentication required", status_code=401)


def admin_only() -> JSONResponse:
    return err(ADMIN_ONLY, "Admin only", status_code=403)
