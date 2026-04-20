"""Tests for app/error_codes.py helpers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestErrHelper:
    def test_basic_shape(self):
        from app.error_codes import err, INPUT_INVALID
        resp = err(INPUT_INVALID, "bad query")
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert body["error"] == "bad query"
        assert body["code"] == "INPUT_INVALID"

    def test_custom_status(self):
        from app.error_codes import err, NOT_FOUND
        resp = err(NOT_FOUND, "gone", status_code=404)
        assert resp.status_code == 404
        body = json.loads(resp.body)
        assert body["code"] == "NOT_FOUND"

    def test_extra_merged(self):
        from app.error_codes import err, RATE_LIMITED
        resp = err(RATE_LIMITED, "slow down", status_code=429,
                   extra={"retry_after": 30, "bucket": "login"})
        body = json.loads(resp.body)
        assert body["retry_after"] == 30
        assert body["bucket"] == "login"
        assert body["code"] == "RATE_LIMITED"


class TestShortcutHelpers:
    def test_auth_required(self):
        from app.error_codes import auth_required
        resp = auth_required()
        assert resp.status_code == 401
        body = json.loads(resp.body)
        assert body["code"] == "AUTH_REQUIRED"

    def test_admin_only(self):
        from app.error_codes import admin_only
        resp = admin_only()
        assert resp.status_code == 403
        body = json.loads(resp.body)
        assert body["code"] == "ADMIN_ONLY"


class TestCatalogStability:
    """All declared constants must be simple SCREAMING_SNAKE strings."""

    def test_codes_are_snake_screaming(self):
        from app import error_codes
        import re
        for attr in dir(error_codes):
            if attr.isupper() and not attr.startswith("_"):
                val = getattr(error_codes, attr)
                if isinstance(val, str):
                    assert re.match(r"^[A-Z][A-Z0-9_]+$", val), \
                        f"{attr} = {val!r} — should be SCREAMING_SNAKE"

    def test_auth_codes_defined(self):
        from app import error_codes
        for name in ("AUTH_REQUIRED", "AUTH_INVALID", "AUTH_BANNED", "ADMIN_ONLY"):
            assert hasattr(error_codes, name)

    def test_subsystem_codes_defined(self):
        from app import error_codes
        for name in ("DB_UNAVAILABLE", "REDIS_UNAVAILABLE", "VOICE_UNAVAILABLE", "LLM_UNAVAILABLE"):
            assert hasattr(error_codes, name)
