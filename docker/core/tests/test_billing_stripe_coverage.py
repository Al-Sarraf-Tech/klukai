"""Behavioral coverage for app.billing_stripe — Stripe webhook ingestion.

Complements tests/test_billing.py (which covers verify_stripe_signature and
the missing-id / replay / unknown-event paths). This file drives the REMAINING
lines: the event-insert failure path, handler dispatch + mark-processed,
handler-raises error capture, and every individual event handler
(_apply_subscription, _cancel_subscription, _record_payment, _mark_past_due)
plus the _stripe_ts / _stripe_obj / _stripe_tier_from_price helpers.

All DB access goes through app.billing_stripe.get_conn_autocommit, which is
patched to a fake async connection. No network, no real psycopg.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import billing_stripe
from app.billing_stripe import (
    _apply_subscription,
    _cancel_subscription,
    _json_dumps,
    _mark_event_processed,
    _mark_past_due,
    _record_payment,
    _stripe_obj,
    _stripe_tier_from_price,
    _stripe_ts,
    _user_id_from_metadata,
    handle_stripe_event,
)


# ── Fake async DB connection ─────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, fetchone_val=None):
        self._fetchone_val = fetchone_val

    async def fetchone(self):
        return self._fetchone_val


class _FakeConn:
    """Records every execute() call so tests can assert on SQL + params."""

    def __init__(self, fetchone_val=None):
        self.calls: list[tuple[str, tuple]] = []
        self._fetchone_val = fetchone_val

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _FakeResult(self._fetchone_val)


def _patch_conn(conn):
    """Return a context manager patching get_conn_autocommit -> conn."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    return patch("app.billing_stripe.get_conn_autocommit", return_value=cm)


# ── Pure helpers ─────────────────────────────────────────────────────────────

class TestPureHelpers:
    def test_json_dumps_serializes_datetime_via_default_str(self):
        out = _json_dumps({"when": datetime(2025, 1, 2, tzinfo=timezone.utc)})
        assert "2025-01-02" in out

    def test_stripe_obj_extracts_nested_object(self):
        assert _stripe_obj({"data": {"object": {"id": "x"}}}) == {"id": "x"}

    def test_stripe_obj_missing_returns_empty_dict(self):
        assert _stripe_obj({}) == {}
        assert _stripe_obj({"data": {"object": None}}) == {}

    def test_stripe_ts_none_returns_none(self):
        assert _stripe_ts(None) is None

    def test_stripe_ts_epoch_to_utc_datetime(self):
        dt = _stripe_ts(0)
        assert dt == datetime(1970, 1, 1, tzinfo=timezone.utc)

    def test_stripe_ts_bad_value_returns_none(self):
        assert _stripe_ts("not-a-number") is None

    def test_user_id_from_metadata_reads_nested_key(self):
        assert _user_id_from_metadata({"metadata": {"user_id": "bob"}}) == "bob"

    def test_tier_from_price_unknown_is_free(self):
        assert _stripe_tier_from_price("price_nope") == "free"


# ── handle_stripe_event dispatch paths ───────────────────────────────────────

class TestHandleStripeEventDispatch:
    @pytest.mark.asyncio
    async def test_insert_failure_returns_not_ok_with_reason(self):
        """If the idempotency INSERT raises, the event is NOT acked (lines 83-85)."""
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=RuntimeError("pool dead"))
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch("app.billing_stripe.get_conn_autocommit", return_value=cm):
            result = await handle_stripe_event({
                "id": "evt_db_down",
                "type": "invoice.paid",
                "data": {"object": {}},
            })
        assert result["ok"] is False
        assert "pool dead" in result["reason"]

    @pytest.mark.asyncio
    async def test_known_event_dispatches_handler_and_marks_processed(self):
        """New event id + known type → handler runs, event marked processed."""
        conn = _FakeConn(fetchone_val=("evt_paid",))
        with _patch_conn(conn):
            result = await handle_stripe_event({
                "id": "evt_paid",
                "type": "invoice.paid",
                "data": {"object": {"id": "in_1", "amount_paid": 999}},
            })
        assert result["ok"] is True
        assert result["handler"] == "_record_payment"
        assert result["result"] == {"invoice": "in_1", "amount_paid": 999}
        # The processed-flag UPDATE must have fired after the handler.
        assert any("processed = TRUE" in sql for sql, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_handler_exception_captured_and_not_acked(self):
        """If a handler raises, error is recorded and ok=False (lines 98-108)."""
        conn = _FakeConn(fetchone_val=("evt_boom",))
        boom = AsyncMock(side_effect=ValueError("handler kaboom"))
        with _patch_conn(conn), \
             patch.object(billing_stripe, "_apply_subscription", boom):
            result = await handle_stripe_event({
                "id": "evt_boom",
                "type": "customer.subscription.created",
                "data": {"object": {}},
            })
        assert result["ok"] is False
        assert "handler kaboom" in result["reason"]
        # The error-recording UPDATE must have been attempted.
        assert any("SET error" in sql for sql, _ in conn.calls)
        # And it must NOT have been marked processed.
        assert not any("processed = TRUE" in sql for sql, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_handler_exception_when_error_logging_also_fails(self):
        """Resilience: if the handler raises AND the error-recording UPDATE
        also blows up, the original failure is still returned, not re-raised
        (covers the nested except/pass, lines 106-107)."""
        # First get_conn_autocommit (idempotency INSERT) succeeds and returns a
        # new event id; the second acquisition (error UPDATE) raises.
        good_conn = _FakeConn(fetchone_val=("evt_nested",))
        good_cm = MagicMock()
        good_cm.__aenter__ = AsyncMock(return_value=good_conn)
        good_cm.__aexit__ = AsyncMock(return_value=None)
        bad_cm = MagicMock()
        bad_cm.__aenter__ = AsyncMock(side_effect=RuntimeError("err-log conn down"))
        bad_cm.__aexit__ = AsyncMock(return_value=None)

        boom = AsyncMock(side_effect=ValueError("primary handler boom"))
        with patch("app.billing_stripe.get_conn_autocommit",
                   side_effect=[good_cm, bad_cm]), \
             patch.object(billing_stripe, "_record_payment", boom):
            result = await handle_stripe_event({
                "id": "evt_nested",
                "type": "invoice.paid",
                "data": {"object": {}},
            })
        # The original handler failure surfaces; the logging failure is swallowed.
        assert result["ok"] is False
        assert "primary handler boom" in result["reason"]


class TestMarkEventProcessed:
    @pytest.mark.asyncio
    async def test_issues_update_with_event_id(self):
        conn = _FakeConn()
        with _patch_conn(conn):
            await _mark_event_processed("evt_42")
        assert len(conn.calls) == 1
        sql, params = conn.calls[0]
        assert "processed = TRUE" in sql
        assert params == ("evt_42",)

    @pytest.mark.asyncio
    async def test_swallows_db_error(self):
        """A failed mark must never propagate (best-effort)."""
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=RuntimeError("down"))
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch("app.billing_stripe.get_conn_autocommit", return_value=cm):
            await _mark_event_processed("evt_x")  # must not raise


# ── _apply_subscription ──────────────────────────────────────────────────────

class TestApplySubscription:
    @pytest.mark.asyncio
    async def test_skips_when_no_user_id(self):
        conn = _FakeConn()
        with _patch_conn(conn):
            res = await _apply_subscription({"data": {"object": {"metadata": {}}}})
        assert res == {"skipped": True, "reason": "no user_id in metadata"}
        assert conn.calls == []  # no DB write when skipped

    @pytest.mark.asyncio
    async def test_upserts_subscription_with_mapped_tier(self, monkeypatch):
        monkeypatch.setenv("STRIPE_PRICE_PRO_MONTHLY", "price_pro_m")
        conn = _FakeConn()
        event = {
            "data": {"object": {
                "id": "sub_1",
                "customer": "cus_1",
                "status": "active",
                "metadata": {"user_id": "alice"},
                "items": {"data": [{"price": {"id": "price_pro_m"}}]},
                "current_period_start": 1700000000,
                "current_period_end": 1702592000,
            }},
        }
        with _patch_conn(conn):
            res = await _apply_subscription(event)
        assert res == {"user_id": "alice", "tier": "pro", "status": "active"}
        sql, params = conn.calls[0]
        assert "INSERT INTO companion_subscriptions" in sql
        # params order: user_id, tier, status, period_start, period_end,
        #               customer_id, subscription_id
        assert params[0] == "alice"
        assert params[1] == "pro"
        assert params[2] == "active"
        assert isinstance(params[3], datetime)  # period_start converted
        assert params[5] == "cus_1"
        assert params[6] == "sub_1"

    @pytest.mark.asyncio
    async def test_no_items_defaults_to_free_tier(self):
        """Missing items list → price_id None → free tier."""
        conn = _FakeConn()
        event = {"data": {"object": {
            "id": "sub_2", "customer": "cus_2", "status": "active",
            "metadata": {"user_id": "carol"}, "items": {},
        }}}
        with _patch_conn(conn):
            res = await _apply_subscription(event)
        assert res["tier"] == "free"
        assert conn.calls[0][1][3] is None  # period_start None -> stored as None


# ── _cancel_subscription ─────────────────────────────────────────────────────

class TestCancelSubscription:
    @pytest.mark.asyncio
    async def test_downgrades_by_user_id(self):
        conn = _FakeConn()
        event = {"data": {"object": {"id": "sub_9", "metadata": {"user_id": "dave"}}}}
        with _patch_conn(conn):
            res = await _cancel_subscription(event)
        assert res == {"user_id": "dave", "subscription_id": "sub_9", "tier": "free"}
        sql, params = conn.calls[0]
        assert "tier = 'free'" in sql and "status = 'canceled'" in sql
        assert "WHERE user_id = %s" in sql
        assert params == ("dave",)

    @pytest.mark.asyncio
    async def test_downgrades_by_subscription_id_when_no_user(self):
        conn = _FakeConn()
        event = {"data": {"object": {"id": "sub_only", "metadata": {}}}}
        with _patch_conn(conn):
            res = await _cancel_subscription(event)
        assert res["user_id"] is None
        assert res["subscription_id"] == "sub_only"
        sql, params = conn.calls[0]
        assert "WHERE stripe_subscription_id = %s" in sql
        assert params == ("sub_only",)

    @pytest.mark.asyncio
    async def test_no_user_and_no_sub_id_issues_no_write(self):
        conn = _FakeConn()
        event = {"data": {"object": {"metadata": {}}}}
        with _patch_conn(conn):
            res = await _cancel_subscription(event)
        assert res["tier"] == "free"
        assert conn.calls == []  # neither branch executed


# ── _record_payment ──────────────────────────────────────────────────────────

class TestRecordPayment:
    @pytest.mark.asyncio
    async def test_returns_invoice_summary_no_db(self):
        res = await _record_payment({"data": {"object": {
            "id": "in_77", "amount_paid": 1500,
        }}})
        assert res == {"invoice": "in_77", "amount_paid": 1500}


# ── _mark_past_due ───────────────────────────────────────────────────────────

class TestMarkPastDue:
    @pytest.mark.asyncio
    async def test_updates_status_for_subscription(self):
        conn = _FakeConn()
        event = {"data": {"object": {"subscription": "sub_pd"}}}
        with _patch_conn(conn):
            res = await _mark_past_due(event)
        assert res == {"subscription_id": "sub_pd", "status": "past_due"}
        sql, params = conn.calls[0]
        assert "status = 'past_due'" in sql
        assert params == ("sub_pd",)

    @pytest.mark.asyncio
    async def test_no_subscription_id_no_write(self):
        conn = _FakeConn()
        with _patch_conn(conn):
            res = await _mark_past_due({"data": {"object": {}}})
        assert res["subscription_id"] is None
        assert conn.calls == []
