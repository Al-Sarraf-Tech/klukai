"""Mutation-hardening behavioral tests for app.audit_chain.

Targets the surviving logic mutants in the three security-critical helpers:
  * compute_row_hash  — HMAC over the canonical row + prev_hash anchor.
  * verify_chain      — replays the chain and reports the first break.
  * _chain_secret     — fail-closed secret resolution with a fixed priority.

These pin *behavior* (not log/error wording): every input field must affect
the hash, distinct fields must not be interchangeable, the chain seed/anchor
must matter, NULL hashes are breaks, the break is reported at the right
row id, and secret resolution honors AUDIT_CHAIN_SECRET > SIGNED_URL_SECRET >
VAPID_PRIVATE_KEY. Mirrors the fixture style of test_security_integrity_fixes.

Equivalent mutants documented inline (see TestVerifyChainReseed and the module
note) are deliberately NOT chased with code changes.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import audit_chain
from app.audit_chain import compute_row_hash, verify_chain

# A fixed, non-default secret so HMAC output is deterministic regardless of the
# ambient environment a parallel mutmut worker may have set.
_TEST_SECRET = "mutant-hunt-fixed-secret-value"


@pytest.fixture(autouse=True)
def _fixed_chain_secret(monkeypatch):
    """Force a known AUDIT_CHAIN_SECRET for every test in this module.

    _chain_secret reads os.environ on each call, so monkeypatching the env var
    makes compute_row_hash deterministic and isolated from other workers.
    """
    monkeypatch.setenv("AUDIT_CHAIN_SECRET", _TEST_SECRET)
    monkeypatch.delenv("SIGNED_URL_SECRET", raising=False)
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    yield


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _base_kwargs(**over):
    """A fully-populated, distinct-valued compute_row_hash kwargs dict.

    Every field carries a *different* string/value so a mutant that swaps two
    dict slots (e.g. user_id↔ip_address) changes the canonical payload and is
    detected by the per-field sensitivity tests below.
    """
    base = dict(
        row_id=7,
        event_type="login.success",
        user_id="alice",
        ip_address="10.0.0.1",
        request_id="req-xyz",
        metadata={"k": "v", "n": 3},
        created_at="2026-06-11T12:34:56Z",
        prev_hash="prev-anchor",
    )
    base.update(over)
    return base


def _chain(n, prev=None, start_id=0, id_step=1):
    """Build n correctly-chained rows (oldest-first), seeded from `prev`.

    `id_step` lets ids diverge from list indices so break_at_id (an id) can be
    distinguished from a mutant that reports the loop index instead.
    """
    rows = []
    for k in range(n):
        rid = start_id + k * id_step
        r = {
            "id": rid,
            "event_type": "t",
            "user_id": "alice",
            "ip_address": None,
            "request_id": None,
            "metadata": None,
            "created_at": f"2026-06-11T00:00:{k:02d}Z",
        }
        h = compute_row_hash(
            row_id=r["id"], event_type=r["event_type"], user_id=r["user_id"],
            ip_address=r["ip_address"], request_id=r["request_id"],
            metadata=r["metadata"], created_at=r["created_at"], prev_hash=prev,
        )
        r["chain_hash"] = h
        rows.append(r)
        prev = h
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# compute_row_hash — determinism + total field sensitivity
# ─────────────────────────────────────────────────────────────────────────────


class TestComputeRowHashDeterminism:
    def test_same_inputs_same_hash(self):
        a = compute_row_hash(**_base_kwargs())
        b = compute_row_hash(**_base_kwargs())
        assert a == b

    def test_output_is_sha256_hex(self):
        h = compute_row_hash(**_base_kwargs())
        assert len(h) == 64
        assert isinstance(h, str)
        int(h, 16)  # valid hex


class TestComputeRowHashFieldSensitivity:
    """Changing ANY single field must change the hash.

    This kills mutants that drop a dict key, or replace a key's value with a
    constant / another field, for every one of the eight inputs — closing the
    gap left by the older 3-field test in test_signed_urls_and_chain.py.
    """

    @pytest.mark.parametrize("field,new_value", [
        ("row_id", 99),
        ("event_type", "login.failure"),
        ("user_id", "bob"),
        ("ip_address", "192.168.1.1"),
        ("request_id", "req-other"),
        ("metadata", {"k": "v", "n": 4}),
        ("created_at", "2099-01-01T00:00:00Z"),
        ("prev_hash", "different-anchor"),
    ])
    def test_field_change_alters_hash(self, field, new_value):
        baseline = compute_row_hash(**_base_kwargs())
        altered = compute_row_hash(**_base_kwargs(**{field: new_value}))
        assert altered != baseline, f"field {field!r} did not affect the hash"

    def test_fields_are_not_interchangeable(self):
        """Swapping two fields' values must change the hash.

        Kills a mutant that maps the wrong argument into a dict slot
        (e.g. "user_id": ip_address). With distinct values the canonical
        payload differs, so the hash must differ.
        """
        baseline = compute_row_hash(**_base_kwargs())
        swapped = compute_row_hash(
            **_base_kwargs(user_id="10.0.0.1", ip_address="alice"))
        assert swapped != baseline

    def test_event_type_swap_with_request_id(self):
        baseline = compute_row_hash(**_base_kwargs())
        swapped = compute_row_hash(
            **_base_kwargs(event_type="req-xyz", request_id="login.success"))
        assert swapped != baseline

    def test_row_id_value_is_bound_not_constant(self):
        """Two different ids (all else equal) must differ — kills id→const."""
        h1 = compute_row_hash(**_base_kwargs(row_id=1))
        h2 = compute_row_hash(**_base_kwargs(row_id=2))
        assert h1 != h2

    def test_metadata_value_is_bound(self):
        h1 = compute_row_hash(**_base_kwargs(metadata={"a": 1}))
        h2 = compute_row_hash(**_base_kwargs(metadata={"a": 2}))
        assert h1 != h2


class TestComputeRowHashPrevAnchor:
    """`prev_hash or ""` normalization — kills the `or`→`and` mutant."""

    def test_none_and_empty_prev_hash_are_equivalent(self):
        h_none = compute_row_hash(**_base_kwargs(prev_hash=None))
        h_empty = compute_row_hash(**_base_kwargs(prev_hash=""))
        assert h_none == h_empty

    def test_real_prev_hash_differs_from_none(self):
        h_none = compute_row_hash(**_base_kwargs(prev_hash=None))
        h_real = compute_row_hash(**_base_kwargs(prev_hash="real-prev"))
        assert h_real != h_none

    def test_distinct_prev_hashes_differ(self):
        h1 = compute_row_hash(**_base_kwargs(prev_hash="p1"))
        h2 = compute_row_hash(**_base_kwargs(prev_hash="p2"))
        assert h1 != h2


class TestComputeRowHashNoneAndSerialization:
    def test_none_optional_fields_do_not_raise(self):
        h = compute_row_hash(
            row_id=1, event_type="e", user_id=None, ip_address=None,
            request_id=None, metadata=None, created_at="c", prev_hash=None)
        assert len(h) == 64

    def test_non_json_metadata_serialized_via_default_str(self):
        """metadata holding a datetime must hash (default=str), not raise.

        Kills removal/alteration of the `default=str` argument.
        """
        dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        h = compute_row_hash(
            row_id=1, event_type="e", user_id=None, ip_address=None,
            request_id=None, metadata={"when": dt}, created_at="c",
            prev_hash=None)
        assert len(h) == 64

    def test_none_user_id_differs_from_empty_string(self):
        """None vs "" must NOT collapse for user_id (no `or ""` on it)."""
        h_none = compute_row_hash(**_base_kwargs(user_id=None))
        h_empty = compute_row_hash(**_base_kwargs(user_id=""))
        assert h_none != h_empty


class TestComputeRowHashSecretBinding:
    """The HMAC key must actually be _chain_secret() (behavioral)."""

    def test_hash_changes_when_secret_changes(self, monkeypatch):
        h1 = compute_row_hash(**_base_kwargs())
        monkeypatch.setenv("AUDIT_CHAIN_SECRET", "a-totally-different-secret")
        h2 = compute_row_hash(**_base_kwargs())
        assert h1 != h2


# ─────────────────────────────────────────────────────────────────────────────
# verify_chain — break detection, counts, anchoring, NULL handling
# ─────────────────────────────────────────────────────────────────────────────


class TestVerifyChainValidPath:
    def test_empty_chain_is_valid_checked_zero(self):
        res = verify_chain([])
        assert res["valid"] is True
        assert res["checked"] == 0
        assert res["break_at_id"] is None

    def test_valid_multi_row_chain(self):
        rows = _chain(4)
        res = verify_chain(rows)
        assert res["valid"] is True
        assert res["break_at_id"] is None

    def test_valid_chain_checked_equals_len(self):
        """`checked` on a clean run is len(rows) exactly — pins the count."""
        rows = _chain(5)
        res = verify_chain(rows)
        assert res["checked"] == 5
        assert res["checked"] != 4
        assert res["checked"] != 6

    def test_valid_chain_expected_hash_is_last_stored(self):
        rows = _chain(3)
        res = verify_chain(rows)
        assert res["expected_hash"] == rows[-1]["chain_hash"]

    def test_single_valid_row(self):
        rows = _chain(1)
        res = verify_chain(rows)
        assert res["valid"] is True
        assert res["checked"] == 1


class TestVerifyChainMismatch:
    def test_tampered_middle_row_detected(self):
        rows = _chain(3)
        rows[1]["event_type"] = "tampered"
        res = verify_chain(rows)
        assert res["valid"] is False
        assert res["reason"] == "hash_mismatch"

    def test_bad_stored_hash_detected(self):
        rows = _chain(2)
        rows[1]["chain_hash"] = "0" * 64
        res = verify_chain(rows)
        assert res["valid"] is False
        assert res["reason"] == "hash_mismatch"

    def test_break_reported_at_row_id_not_index(self):
        """break_at_id must be the row's id, not the loop index.

        ids are 100, 107, 114 (id_step=7); tampering the middle row must
        report 107 and checked == 1. Kills a `r["id"]`→`idx` mutant and an
        `enumerate(rows, 1)` start mutant simultaneously.
        """
        rows = _chain(3, start_id=100, id_step=7)
        rows[1]["event_type"] = "tampered"
        res = verify_chain(rows)
        assert res["break_at_id"] == 107
        assert res["checked"] == 1

    def test_first_row_break_checked_is_zero(self):
        """Break on row 0 → checked == 0. Kills enumerate(start=1)."""
        rows = _chain(3, start_id=100, id_step=7)
        rows[0]["event_type"] = "tampered"
        res = verify_chain(rows)
        assert res["valid"] is False
        assert res["break_at_id"] == 100
        assert res["checked"] == 0

    def test_break_expected_hash_is_recomputed_value(self):
        """On a break, expected_hash is the freshly computed hash for that row.

        Distinct from the (tampered) stored hash, so it must equal the
        recomputation, not the stored "0"*64.
        """
        rows = _chain(2, start_id=50, id_step=3)
        rows[1]["chain_hash"] = "0" * 64
        res = verify_chain(rows)
        recomputed = compute_row_hash(
            row_id=rows[1]["id"], event_type=rows[1]["event_type"],
            user_id=rows[1]["user_id"], ip_address=rows[1]["ip_address"],
            request_id=rows[1]["request_id"], metadata=rows[1]["metadata"],
            created_at=str(rows[1]["created_at"] or ""),
            prev_hash=rows[0]["chain_hash"],
        )
        assert res["expected_hash"] == recomputed
        assert res["expected_hash"] != "0" * 64

    def test_stops_at_first_break(self):
        """With two corrupted rows, the FIRST (lower idx) is reported."""
        rows = _chain(4, start_id=10, id_step=5)
        rows[2]["event_type"] = "X"
        rows[3]["event_type"] = "Y"
        res = verify_chain(rows)
        assert res["break_at_id"] == 20  # row idx 2 → id 10 + 2*5
        assert res["checked"] == 2


class TestVerifyChainNullHash:
    """NULL/missing chain_hash is ALWAYS a break (never a skip/re-anchor)."""

    def test_null_hash_is_break_not_valid(self):
        rows = _chain(3)
        rows[1]["chain_hash"] = None
        res = verify_chain(rows)
        assert res["valid"] is False
        assert res["reason"] == "missing_chain_hash"

    def test_empty_string_hash_is_break(self):
        """`if not stored` treats "" as falsy → break (kills `is None` swap)."""
        rows = _chain(2)
        rows[0]["chain_hash"] = ""
        res = verify_chain(rows)
        assert res["valid"] is False
        assert res["break_at_id"] == rows[0]["id"]
        assert res["reason"] == "missing_chain_hash"

    def test_missing_chain_hash_key_is_break(self):
        rows = _chain(2)
        del rows[0]["chain_hash"]
        res = verify_chain(rows)
        assert res["valid"] is False
        assert res["reason"] == "missing_chain_hash"

    def test_null_hash_reports_correct_id_and_checked(self):
        rows = _chain(3, start_id=200, id_step=9)
        rows[2]["chain_hash"] = None
        res = verify_chain(rows)
        assert res["break_at_id"] == 218  # 200 + 2*9
        assert res["checked"] == 2

    def test_none_created_at_is_coalesced_to_empty_string(self):
        """verify_chain normalizes a NULL created_at via `... or ""`.

        A row stored with created_at=None whose hash was computed over
        created_at="" must verify. Kills a mutant that drops the `or ""`
        (which would hash str(None)=="None" and produce a spurious mismatch).
        """
        h = compute_row_hash(
            row_id=1, event_type="e", user_id=None, ip_address=None,
            request_id=None, metadata=None, created_at="", prev_hash=None)
        row = {
            "id": 1, "event_type": "e", "user_id": None, "ip_address": None,
            "request_id": None, "metadata": None, "created_at": None,
            "chain_hash": h,
        }
        res = verify_chain([row])
        assert res["valid"] is True

    def test_null_row_does_not_reanchor_following_rows(self):
        """A NULLed row must not let later (validly-chained) rows pass.

        rows 1-2 are chained off row 0's ORIGINAL hash; if a mutant
        re-anchored prev from the recomputed hash of the NULL row, rows 1-2
        could verify. The NULL on row 0 must short-circuit to a break at id 0.
        """
        rows = _chain(3)
        rows[0]["chain_hash"] = None
        res = verify_chain(rows)
        assert res["valid"] is False
        assert res["break_at_id"] == 0


class TestVerifyChainAnchor:
    """`prev_hash` seeds the chain from a trust anchor."""

    def test_correct_anchor_verifies_segment(self):
        rows = _chain(5)
        anchor = rows[1]["chain_hash"]
        res = verify_chain(rows[2:], prev_hash=anchor)
        assert res["valid"] is True
        assert res["checked"] == 3

    def test_wrong_anchor_breaks_at_first_row(self):
        rows = _chain(5)
        res = verify_chain(rows[2:], prev_hash="bogus-anchor")
        assert res["valid"] is False
        assert res["break_at_id"] == rows[2]["id"]
        assert res["reason"] == "hash_mismatch"

    def test_anchor_actually_seeds_first_row(self):
        """A chain built off a non-empty anchor fails when verified as genesis.

        Proves the anchor parameter feeds prev_hash for rows[0] (kills a
        mutant that ignores the seed / starts from None).
        """
        rows = _chain(3, prev="REAL-ANCHOR")
        # Verified with the right anchor → valid.
        assert verify_chain(rows, prev_hash="REAL-ANCHOR")["valid"] is True
        # Verified as genesis (prev_hash defaults to None) → break at row 0.
        res = verify_chain(rows)
        assert res["valid"] is False
        assert res["break_at_id"] == rows[0]["id"]

    def test_default_anchor_is_none_genesis(self):
        """No anchor arg ⇒ rows[0] treated as genesis (prev_hash="")."""
        genesis_rows = _chain(2, prev=None)
        assert verify_chain(genesis_rows)["valid"] is True


class TestVerifyChainReseed:
    """Documents the stored-vs-computed reseed mutant as EQUIVALENT.

    The line `prev_hash = stored` could be mutated to `prev_hash = computed`.
    Execution only reaches that line after the `stored != computed` guard has
    passed, i.e. when stored == computed — so both assignments store the
    identical value and no observable behavior changes. This is a genuine
    EQUIVALENT MUTANT; it is not chased with code contortions. The test below
    locks in the property the assignment must preserve: a long valid chain (in
    which every reseed runs) verifies end-to-end.
    """

    def test_long_valid_chain_threads_prev_through_every_row(self):
        rows = _chain(8)
        res = verify_chain(rows)
        assert res["valid"] is True
        assert res["checked"] == 8

    def test_tampering_after_many_valid_rows_still_detected(self):
        rows = _chain(8, start_id=0, id_step=2)
        rows[6]["event_type"] = "tampered"
        res = verify_chain(rows)
        assert res["valid"] is False
        assert res["break_at_id"] == 12  # idx 6 → 0 + 6*2
        assert res["checked"] == 6


# ─────────────────────────────────────────────────────────────────────────────
# _chain_secret — resolution order + fail-closed, returns bytes
# ─────────────────────────────────────────────────────────────────────────────


class TestChainSecretResolution:
    def test_returns_bytes(self, monkeypatch):
        monkeypatch.setenv("AUDIT_CHAIN_SECRET", "whatever")
        assert isinstance(audit_chain._chain_secret(), bytes)

    def test_audit_chain_secret_takes_priority(self, monkeypatch):
        monkeypatch.setenv("AUDIT_CHAIN_SECRET", "primary")
        monkeypatch.setenv("SIGNED_URL_SECRET", "secondary")
        monkeypatch.setenv("VAPID_PRIVATE_KEY", "tertiary")
        assert audit_chain._chain_secret() == b"primary"

    def test_signed_url_secret_is_second(self, monkeypatch):
        monkeypatch.delenv("AUDIT_CHAIN_SECRET", raising=False)
        monkeypatch.setenv("SIGNED_URL_SECRET", "secondary")
        monkeypatch.setenv("VAPID_PRIVATE_KEY", "tertiary")
        assert audit_chain._chain_secret() == b"secondary"

    def test_vapid_key_is_last_fallback(self, monkeypatch):
        monkeypatch.delenv("AUDIT_CHAIN_SECRET", raising=False)
        monkeypatch.delenv("SIGNED_URL_SECRET", raising=False)
        monkeypatch.setenv("VAPID_PRIVATE_KEY", "tertiary")
        assert audit_chain._chain_secret() == b"tertiary"

    def test_value_is_utf8_encoding_of_resolved_string(self, monkeypatch):
        monkeypatch.setenv("AUDIT_CHAIN_SECRET", "ünïcödé-key")
        assert audit_chain._chain_secret() == "ünïcödé-key".encode("utf-8")

    def test_reads_env_live_no_caching(self, monkeypatch):
        monkeypatch.setenv("AUDIT_CHAIN_SECRET", "first")
        assert audit_chain._chain_secret() == b"first"
        monkeypatch.setenv("AUDIT_CHAIN_SECRET", "second")
        assert audit_chain._chain_secret() == b"second"

    def test_fail_closed_dev_placeholder_under_pytest(self, monkeypatch):
        """No env secret + pytest context ⇒ deterministic dev placeholder.

        It must be tied to the audit-chain purpose (kills a `purpose=` keyword
        mutant) and must NOT be a guessable legacy literal like 'dev-secret'.
        It must also be stable within the process (so sign/verify round-trips).
        """
        monkeypatch.delenv("AUDIT_CHAIN_SECRET", raising=False)
        monkeypatch.delenv("SIGNED_URL_SECRET", raising=False)
        monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
        sec = audit_chain._chain_secret()
        assert isinstance(sec, bytes)
        assert b"audit-chain" in sec
        assert sec != b"dev-secret"
        assert audit_chain._chain_secret() == sec  # stable
