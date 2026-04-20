"""Tests for signed_urls.py and audit_chain.py."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─────────────────────────────────────────────────────────────────────────
# signed_urls
# ─────────────────────────────────────────────────────────────────────────


class TestSignedUrls:
    def test_sign_and_verify_happy_path(self):
        from app.signed_urls import sign, verify
        token = sign("resource-123", ttl_seconds=60, user_id="alice")
        assert verify(token, "resource-123", user_id="alice") is True

    def test_verify_wrong_resource_id_fails(self):
        from app.signed_urls import sign, verify
        token = sign("resource-123", ttl_seconds=60)
        assert verify(token, "resource-OTHER") is False

    def test_verify_wrong_user_fails(self):
        from app.signed_urls import sign, verify
        token = sign("r1", ttl_seconds=60, user_id="alice")
        assert verify(token, "r1", user_id="bob") is False

    def test_expired_token_fails(self):
        from app.signed_urls import sign, verify
        token = sign("r1", ttl_seconds=1, user_id="alice")
        time.sleep(1.1)
        assert verify(token, "r1", user_id="alice") is False

    def test_malformed_token_fails(self):
        from app.signed_urls import verify
        assert verify("garbage", "r1") is False
        assert verify("", "r1") is False
        assert verify("no-dot-at-all", "r1") is False
        assert verify("abc.def", "r1") is False  # exp not int

    def test_different_secret_rejects_token(self, monkeypatch):
        from app.signed_urls import sign, verify
        monkeypatch.setenv("SIGNED_URL_SECRET", "secret-A")
        token = sign("r1")
        monkeypatch.setenv("SIGNED_URL_SECRET", "secret-B")
        assert verify(token, "r1") is False

    def test_signed_path_appends_sig_param(self):
        from app.signed_urls import signed_path
        assert "sig=" in signed_path("/api/memories/123/image", "123")

    def test_signed_path_respects_existing_query(self):
        from app.signed_urls import signed_path
        p = signed_path("/x?foo=bar", "123")
        assert "foo=bar" in p
        assert "sig=" in p
        assert "?sig=" not in p  # used & because foo=bar already there

    def test_ttl_zero_clamped_to_one(self):
        """ttl_seconds <= 0 must still produce a (very-short) valid token."""
        from app.signed_urls import sign, verify
        token = sign("r1", ttl_seconds=0)
        # The token was clamped to 1s — verify should succeed immediately
        assert verify(token, "r1") is True


# ─────────────────────────────────────────────────────────────────────────
# audit_chain
# ─────────────────────────────────────────────────────────────────────────


class TestAuditChain:
    def test_hash_is_deterministic(self):
        from app.audit_chain import compute_row_hash
        args = dict(
            row_id=1, event_type="login.success", user_id="alice",
            ip_address="1.2.3.4", request_id="req-1",
            metadata={"username": "alice"},
            created_at="2026-04-20T00:00:00Z", prev_hash=None,
        )
        h1 = compute_row_hash(**args)
        h2 = compute_row_hash(**args)
        assert h1 == h2
        assert len(h1) == 64  # sha256 hex

    def test_hash_changes_with_any_field(self):
        from app.audit_chain import compute_row_hash
        base = dict(
            row_id=1, event_type="x", user_id=None, ip_address=None,
            request_id=None, metadata=None,
            created_at="2026-04-20T00:00:00Z", prev_hash=None,
        )
        baseline = compute_row_hash(**base)
        # Changing each field changes the hash
        for field, new in [("event_type", "y"), ("user_id", "alice"),
                           ("prev_hash", "abc")]:
            alt = dict(base)
            alt[field] = new
            assert compute_row_hash(**alt) != baseline, f"field {field} did not alter hash"

    def test_verify_chain_empty_list_valid(self):
        from app.audit_chain import verify_chain
        result = verify_chain([])
        assert result["valid"] is True
        assert result["checked"] == 0

    def test_verify_chain_with_correct_stored_hashes(self):
        from app.audit_chain import compute_row_hash, verify_chain
        prev = None
        rows = []
        for i in range(3):
            r = {
                "id": i, "event_type": "t", "user_id": "alice",
                "ip_address": None, "request_id": None, "metadata": None,
                "created_at": f"2026-04-20T00:00:0{i}Z",
            }
            h = compute_row_hash(
                row_id=r["id"], event_type=r["event_type"],
                user_id=r["user_id"], ip_address=r["ip_address"],
                request_id=r["request_id"], metadata=r["metadata"],
                created_at=r["created_at"], prev_hash=prev,
            )
            r["chain_hash"] = h
            rows.append(r)
            prev = h

        result = verify_chain(rows)
        assert result["valid"] is True
        assert result["break_at_id"] is None

    def test_verify_chain_detects_tampered_row(self):
        from app.audit_chain import compute_row_hash, verify_chain
        prev = None
        rows = []
        for i in range(3):
            r = {
                "id": i, "event_type": "t", "user_id": "alice",
                "ip_address": None, "request_id": None, "metadata": None,
                "created_at": f"2026-04-20T00:00:0{i}Z",
            }
            h = compute_row_hash(
                row_id=r["id"], event_type=r["event_type"],
                user_id=r["user_id"], ip_address=r["ip_address"],
                request_id=r["request_id"], metadata=r["metadata"],
                created_at=r["created_at"], prev_hash=prev,
            )
            r["chain_hash"] = h
            rows.append(r)
            prev = h

        # Tamper: change row 1's event_type (but leave its chain_hash)
        rows[1]["event_type"] = "tampered"

        result = verify_chain(rows)
        assert result["valid"] is False
        assert result["break_at_id"] == 1

    def test_verify_chain_detects_bad_stored_hash(self):
        from app.audit_chain import compute_row_hash, verify_chain
        prev = None
        rows = []
        for i in range(2):
            r = {
                "id": i, "event_type": "t", "user_id": None,
                "ip_address": None, "request_id": None, "metadata": None,
                "created_at": f"2026-04-20T00:00:0{i}Z",
            }
            h = compute_row_hash(
                row_id=r["id"], event_type=r["event_type"],
                user_id=r["user_id"], ip_address=r["ip_address"],
                request_id=r["request_id"], metadata=r["metadata"],
                created_at=r["created_at"], prev_hash=prev,
            )
            r["chain_hash"] = h
            rows.append(r)
            prev = h

        # Tamper the stored hash on row 1
        rows[1]["chain_hash"] = "0" * 64

        result = verify_chain(rows)
        assert result["valid"] is False
        assert result["break_at_id"] == 1
