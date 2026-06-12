"""Security/integrity regression tests — 2026-06-11 audit fixes.

Covers six verified bugs:
1. audit_chain.verify_chain treated NULL chain_hash rows as valid links
   (attacker NULLs chain_hash after tampering → chain trivially forgeable).
2. /api/audit/verify-chain verified the OLDEST N rows, so recent tampering
   was never checked.
3. audit.log read-then-insert race could fork the hash chain — now
   serialized with pg_advisory_xact_lock.
4. Stripe webhook idempotency: a retried event whose first attempt failed
   (processed = FALSE) was treated as a replay and dropped forever; the
   route returned 200 even when the handler failed.
5. Tribute 24h cooldown was check-then-act and count_recent failed OPEN
   (DB error → 0 → +20 affection repeatable). Now atomic + fail-closed.
6. /api/user/change-password claimed rate limiting but was not in
   _RATE_LIMIT_BUCKETS.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import tributes
from app.audit_chain import compute_row_hash, verify_chain


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _chain_rows(n: int, prev: str | None = None, start_id: int = 0):
    """Build n correctly-chained rows (oldest-first), seeded from `prev`."""
    rows = []
    for i in range(start_id, start_id + n):
        r = {
            "id": i, "event_type": "t", "user_id": "alice",
            "ip_address": None, "request_id": None, "metadata": None,
            "created_at": f"2026-06-11T00:00:{i:02d}Z",
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


def _find_route(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route {method} {path} not found")


def _mk_request(token: str | None = "tok"):
    req = MagicMock()
    req.headers = {"Authorization": f"Bearer {token}"} if token else {}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    req.state = MagicMock()
    req.state.request_id = "rq-test"
    return req


# ─────────────────────────────────────────────────────────────────────────────
# Fix 1 — verify_chain must treat NULL chain_hash as a break
# ─────────────────────────────────────────────────────────────────────────────


class TestVerifyChainNullHash:
    def test_null_chain_hash_is_a_break(self):
        """Attacker tampers a row then NULLs its chain_hash — must be flagged."""
        rows = _chain_rows(3)
        rows[1]["event_type"] = "tampered"
        rows[1]["chain_hash"] = None
        result = verify_chain(rows)
        assert result["valid"] is False
        assert result["break_at_id"] == 1

    def test_null_hash_without_tampering_still_breaks(self):
        """Even an untampered NULL-hash row must surface (writer hash failure)."""
        rows = _chain_rows(3)
        rows[2]["chain_hash"] = None
        result = verify_chain(rows)
        assert result["valid"] is False
        assert result["break_at_id"] == 2
        assert result.get("reason") == "missing_chain_hash"

    def test_all_nulls_invalid(self):
        rows = _chain_rows(2)
        for r in rows:
            r["chain_hash"] = None
        result = verify_chain(rows)
        assert result["valid"] is False
        assert result["break_at_id"] == 0

    def test_never_reseeds_prev_from_computed_on_null_row(self):
        """A NULL row must not silently re-anchor the chain for later rows."""
        rows = _chain_rows(3)
        # NULL row 0's hash AND tamper it; rows 1-2 chain off the ORIGINAL
        # hash so they'd verify if prev were reseeded from the recomputation.
        rows[0]["chain_hash"] = None
        result = verify_chain(rows)
        assert result["valid"] is False
        assert result["break_at_id"] == 0

    def test_mismatch_reports_reason(self):
        rows = _chain_rows(2)
        rows[1]["chain_hash"] = "0" * 64
        result = verify_chain(rows)
        assert result["valid"] is False
        assert result.get("reason") == "hash_mismatch"

    def test_valid_chain_still_passes(self):
        rows = _chain_rows(4)
        result = verify_chain(rows)
        assert result["valid"] is True
        assert result["checked"] == 4

    def test_anchor_prev_hash_param(self):
        """verify_chain can verify a chain segment from a trust anchor."""
        rows = _chain_rows(5)
        anchor = rows[1]["chain_hash"]
        result = verify_chain(rows[2:], prev_hash=anchor)
        assert result["valid"] is True
        assert result["checked"] == 3

    def test_wrong_anchor_fails(self):
        rows = _chain_rows(5)
        result = verify_chain(rows[2:], prev_hash="bogus")
        assert result["valid"] is False
        assert result["break_at_id"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# Fix 2 — /api/audit/verify-chain checks the MOST RECENT N rows
# ─────────────────────────────────────────────────────────────────────────────


class _ChainConn:
    def __init__(self, rows, captured):
        self._rows = rows
        self.captured = captured

    async def execute(self, sql, params):
        self.captured.append((sql, params))
        result = AsyncMock()
        result.fetchall = AsyncMock(return_value=self._rows)
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _ChainPool:
    def __init__(self, rows, captured):
        self._rows = rows
        self._captured = captured

    def connection(self):
        return _ChainConn(self._rows, self._captured)


def _extras2_app():
    from fastapi import FastAPI
    from app.routes_extras2 import register_extras2
    app = FastAPI()
    register_extras2(app)
    return app


class TestVerifyChainRouteRecentRows:
    @pytest.mark.asyncio
    async def test_fetches_most_recent_rows_desc(self):
        """The SQL must order DESC (newest) and fetch limit+1 for the anchor."""
        app = _extras2_app()
        handler = _find_route(app, "/api/audit/verify-chain", "GET")
        captured: list = []
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="jalsarraf")), \
             patch("app.routes_extras2.get_pool", return_value=_ChainPool([], captured)), \
             patch("app.routes_extras2.is_admin", return_value=True):
            result = await handler(_mk_request(), limit=100)
        assert result["valid"] is True
        sql, params = captured[0]
        assert "DESC" in sql
        assert params[0] == 101  # limit + 1 anchor row

    @pytest.mark.asyncio
    async def test_detects_recent_tampering(self):
        """Tampering in the newest row must be detected (old code never saw it)."""
        chain = _chain_rows(6)
        chain[-1]["event_type"] = "tampered"
        # DB returns newest-first (DESC), only the last limit+1 = 4 rows.
        recent = chain[-4:][::-1]
        raw = [
            (r["id"], r["event_type"], r["user_id"], r["ip_address"],
             r["request_id"], r["metadata"], r["created_at"], r["chain_hash"])
            for r in recent
        ]
        app = _extras2_app()
        handler = _find_route(app, "/api/audit/verify-chain", "GET")
        captured: list = []
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="jalsarraf")), \
             patch("app.routes_extras2.get_pool", return_value=_ChainPool(raw, captured)), \
             patch("app.routes_extras2.is_admin", return_value=True):
            result = await handler(_mk_request(), limit=3)
        assert result["valid"] is False
        assert result["break_at_id"] == chain[-1]["id"]

    @pytest.mark.asyncio
    async def test_valid_recent_window_passes_with_anchor(self):
        """A correct chain segment verifies using the extra anchor row."""
        chain = _chain_rows(6)
        recent = chain[-4:][::-1]  # newest-first, anchor included
        raw = [
            (r["id"], r["event_type"], r["user_id"], r["ip_address"],
             r["request_id"], r["metadata"], r["created_at"], r["chain_hash"])
            for r in recent
        ]
        app = _extras2_app()
        handler = _find_route(app, "/api/audit/verify-chain", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="jalsarraf")), \
             patch("app.routes_extras2.get_pool", return_value=_ChainPool(raw, [])), \
             patch("app.routes_extras2.is_admin", return_value=True):
            result = await handler(_mk_request(), limit=3)
        assert result["valid"] is True
        assert result["checked"] == 3

    @pytest.mark.asyncio
    async def test_whole_table_smaller_than_limit_verifies_from_genesis(self):
        chain = _chain_rows(2)
        raw = [
            (r["id"], r["event_type"], r["user_id"], r["ip_address"],
             r["request_id"], r["metadata"], r["created_at"], r["chain_hash"])
            for r in chain[::-1]
        ]
        app = _extras2_app()
        handler = _find_route(app, "/api/audit/verify-chain", "GET")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="jalsarraf")), \
             patch("app.routes_extras2.get_pool", return_value=_ChainPool(raw, [])), \
             patch("app.routes_extras2.is_admin", return_value=True):
            result = await handler(_mk_request(), limit=100)
        assert result["valid"] is True
        assert result["checked"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# Fix 3 — audit.log serializes chain writes via advisory lock
# ─────────────────────────────────────────────────────────────────────────────


class _RecordingConn:
    def __init__(self, fail_hash=False):
        self.sqls: list[str] = []
        self.params: list = []
        self._fail_hash = fail_hash

    async def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.params.append(params)
        result = AsyncMock()
        if "SELECT chain_hash" in sql:
            result.fetchone = AsyncMock(return_value=("prevhash",))
        elif "RETURNING" in sql:
            result.fetchone = AsyncMock(
                return_value=(7, datetime(2026, 6, 11, tzinfo=timezone.utc)))
        else:
            result.fetchone = AsyncMock(return_value=None)
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _RecordingPool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return self._conn


class TestAuditLogAdvisoryLock:
    @pytest.mark.asyncio
    async def test_takes_advisory_lock_before_reading_prev_hash(self):
        from app.audit import log
        conn = _RecordingConn()
        with patch("app.audit.get_pool", return_value=_RecordingPool(conn)):
            await log("login.success", user_id="alice")
        lock_idx = next(
            (i for i, s in enumerate(conn.sqls) if "pg_advisory_xact_lock" in s), None)
        sel_idx = next(
            (i for i, s in enumerate(conn.sqls) if "SELECT chain_hash" in s), None)
        assert lock_idx is not None, "no pg_advisory_xact_lock taken"
        assert sel_idx is not None
        assert lock_idx < sel_idx, "lock must be taken before reading prev hash"

    @pytest.mark.asyncio
    async def test_lock_key_is_parameterized(self):
        from app.audit import log
        conn = _RecordingConn()
        with patch("app.audit.get_pool", return_value=_RecordingPool(conn)):
            await log("login.success")
        for sql, params in zip(conn.sqls, conn.params):
            if "pg_advisory_xact_lock" in sql:
                assert "%s" in sql
                assert params and isinstance(params[0], int)
                return
        raise AssertionError("lock SQL not found")

    @pytest.mark.asyncio
    async def test_hash_failure_logged_as_error(self, caplog):
        """Writer must fail LOUDLY (log.error) when chain hashing fails."""
        import logging
        from app.audit import log
        conn = _RecordingConn()
        with patch("app.audit.get_pool", return_value=_RecordingPool(conn)), \
             patch("app.audit_chain.compute_row_hash",
                   side_effect=RuntimeError("hmac boom")), \
             caplog.at_level(logging.ERROR, logger="app.audit"):
            await log("gift.given", user_id="alice")
        assert any(r.levelno >= logging.ERROR for r in caplog.records), \
            "chain hash failure must be logged at ERROR"

    @pytest.mark.asyncio
    async def test_row_still_inserted_on_hash_failure(self):
        from app.audit import log
        conn = _RecordingConn()
        with patch("app.audit.get_pool", return_value=_RecordingPool(conn)), \
             patch("app.audit_chain.compute_row_hash",
                   side_effect=RuntimeError("hmac boom")):
            await log("gift.given", user_id="alice")
        assert any("INSERT INTO companion_audit_log" in s for s in conn.sqls)


# ─────────────────────────────────────────────────────────────────────────────
# Fix 4 — Stripe webhook idempotency re-dispatch + 500 on failure
# ─────────────────────────────────────────────────────────────────────────────


class _StripeConn:
    """fetchone returns values from a queue, one per execute."""

    def __init__(self, fetchones):
        self.calls: list[tuple[str, tuple]] = []
        self._queue = list(fetchones)

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        result = AsyncMock()
        val = self._queue.pop(0) if self._queue else None
        result.fetchone = AsyncMock(return_value=val)
        return result


def _patch_stripe_conn(conn):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    return patch("app.billing_stripe.get_conn_autocommit", return_value=cm)


class TestStripeIdempotencyRedispatch:
    @pytest.mark.asyncio
    async def test_unprocessed_conflict_redispatches_handler(self):
        """Retry of an event whose first attempt failed must re-run the handler."""
        from app.billing_stripe import handle_stripe_event
        # INSERT conflicts (None), SELECT processed → (False,)
        conn = _StripeConn([None, (False,)])
        handler = AsyncMock(return_value={"done": True})
        with _patch_stripe_conn(conn), \
             patch.dict("app.billing_stripe.__dict__",
                        {"_record_payment": handler}):
            result = await handle_stripe_event({
                "id": "evt_retry", "type": "invoice.paid",
                "data": {"object": {}},
            })
        assert result["ok"] is True
        assert result.get("replay") is not True
        handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_processed_conflict_is_replay(self):
        from app.billing_stripe import handle_stripe_event
        conn = _StripeConn([None, (True,)])
        with _patch_stripe_conn(conn):
            result = await handle_stripe_event({
                "id": "evt_done", "type": "invoice.paid",
                "data": {"object": {}},
            })
        assert result["ok"] is True
        assert result.get("replay") is True

    @pytest.mark.asyncio
    async def test_conflict_with_missing_row_is_not_ok(self):
        """Insert conflicted but lookup found nothing — fail so Stripe retries."""
        from app.billing_stripe import handle_stripe_event
        conn = _StripeConn([None, None])
        with _patch_stripe_conn(conn):
            result = await handle_stripe_event({
                "id": "evt_ghost", "type": "invoice.paid",
                "data": {"object": {}},
            })
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_webhook_route_returns_500_when_handler_fails(self):
        from fastapi import FastAPI
        from app.routes_extras3 import register_extras3
        app = FastAPI()
        register_extras3(app)
        handler = _find_route(app, "/api/billing/webhook", "POST")
        req = MagicMock()
        req.body = AsyncMock(return_value=b'{"id": "evt_x", "type": "invoice.paid"}')
        req.headers = {"Stripe-Signature": "ok"}
        with patch("app.billing.verify_stripe_signature", return_value=True), \
             patch("app.billing.handle_stripe_event",
                   new=AsyncMock(return_value={"ok": False, "reason": "boom"})):
            resp = await handler(req)
        assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_webhook_route_200_on_success(self):
        from fastapi import FastAPI
        from app.routes_extras3 import register_extras3
        app = FastAPI()
        register_extras3(app)
        handler = _find_route(app, "/api/billing/webhook", "POST")
        req = MagicMock()
        req.body = AsyncMock(return_value=b'{"id": "evt_y", "type": "invoice.paid"}')
        req.headers = {"Stripe-Signature": "ok"}
        with patch("app.billing.verify_stripe_signature", return_value=True), \
             patch("app.billing.handle_stripe_event",
                   new=AsyncMock(return_value={"ok": True, "handler": "_record_payment"})):
            result = await handler(req)
        assert result["ok"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Fix 5 — tribute cooldown: fail-closed count + atomic insert
# ─────────────────────────────────────────────────────────────────────────────


class TestTributeCooldownFailClosed:
    @pytest.mark.asyncio
    async def test_count_recent_returns_none_on_db_error(self):
        """DB error must NOT look like 'no recent tributes' (fail-closed)."""
        with patch("app.tributes.get_pool", side_effect=RuntimeError("db down")):
            count = await tributes.count_recent("jalsarraf")
        assert count is None

    @pytest.mark.asyncio
    async def test_route_returns_503_when_count_unavailable(self):
        app = _extras2_app()
        handler = _find_route(app, "/api/tribute", "POST")
        from app.routes import TributeRequest
        req_body = TributeRequest(text="A heartfelt message that is long enough.")
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.tributes.count_recent", new=AsyncMock(return_value=None)):
            resp = await handler(req_body, _mk_request())
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_save_tribute_sql_is_atomic_insert_where_not_exists(self):
        """The INSERT itself must enforce the cooldown (no check-then-act)."""
        pool = MagicMock()
        result_mock = AsyncMock()
        result_mock.fetchone = AsyncMock(return_value=("uuid-1",))
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=result_mock)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=None)
        pool.connection = MagicMock(return_value=ctx)
        with patch("app.tributes.get_pool", return_value=pool):
            tid = await tributes.save_tribute(
                user_id="alice", text="x" * 30)
        assert tid == "uuid-1"
        insert_sql = next(
            (c.args[0] for c in conn.execute.call_args_list
             if "INSERT INTO companion_tributes" in c.args[0]), None)
        assert insert_sql is not None
        assert "NOT EXISTS" in insert_sql
        assert "make_interval" in insert_sql
        assert "%s" in insert_sql  # parameterized

    @pytest.mark.asyncio
    async def test_save_tribute_raises_cooldown_when_insert_blocked(self):
        pool = MagicMock()
        result_mock = AsyncMock()
        result_mock.fetchone = AsyncMock(return_value=None)  # WHERE NOT EXISTS blocked
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=result_mock)
        conn.rollback = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=None)
        pool.connection = MagicMock(return_value=ctx)
        with patch("app.tributes.get_pool", return_value=pool):
            with pytest.raises(tributes.TributeCooldownActive):
                await tributes.save_tribute(user_id="alice", text="x" * 30)

    @pytest.mark.asyncio
    async def test_route_returns_429_on_atomic_cooldown_race(self):
        """count says ok, but atomic insert loses the race → 429, no affection."""
        app = _extras2_app()
        handler = _find_route(app, "/api/tribute", "POST")
        from app.routes import TributeRequest
        req_body = TributeRequest(text="A heartfelt message that is long enough.")
        bump = AsyncMock()
        with patch("app.routes_extras2._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.tributes.count_recent", new=AsyncMock(return_value=0)), \
             patch("app.routes_extras2.affection") as aff, \
             patch("app.tributes.save_tribute",
                   new=AsyncMock(side_effect=tributes.TributeCooldownActive())):
            aff.get_state = AsyncMock(return_value=MagicMock(score=50))
            aff.add_score = bump
            resp = await handler(req_body, _mk_request())
        assert resp.status_code == 429
        bump.assert_not_awaited()  # no +20 affection on the raced attempt


# ─────────────────────────────────────────────────────────────────────────────
# Fix 6 — change-password rate limited via login bucket
# ─────────────────────────────────────────────────────────────────────────────


class TestChangePasswordRateLimit:
    def test_change_password_in_buckets(self):
        from app.main import _RATE_LIMIT_BUCKETS
        assert _RATE_LIMIT_BUCKETS.get("/api/user/change-password") == "login"

    def test_bucket_for_path_resolves_change_password(self):
        from app.main import _bucket_for_path
        assert _bucket_for_path("/api/user/change-password") == "login"
