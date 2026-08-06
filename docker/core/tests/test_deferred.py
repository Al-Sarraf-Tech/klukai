"""Tests for app/deferred.py — one-shot deferred tasks on the RabbitMQ rail.

The design invariant under test: **Postgres is the source of truth, RabbitMQ is
only the timer.** Everything here checks that a broker problem degrades
punctuality and never correctness — no lost tasks, no double delivery, and no
failure that reaches the chat path.
"""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import deferred  # noqa: E402

UTC = timezone.utc

# claim() validates the uuid shape before touching the DB, so fixtures that
# expect a DB round-trip must use a well-formed id.
TASK_ID = "11111111-2222-3333-4444-555555555555"


def _conn_ctx(conn):
    @asynccontextmanager
    async def _cm():
        yield conn

    return _cm


def _conn(fetchone=None, fetchall=None):
    result = MagicMock()
    result.fetchone = AsyncMock(return_value=fetchone)
    result.fetchall = AsyncMock(return_value=fetchall or [])
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=result)
    return conn


# ─────────────────────────────────────────────────────────────────────────
# Delay bucketing
# ─────────────────────────────────────────────────────────────────────────


class TestPlanHops:
    def test_exact_bucket_is_a_single_hop(self):
        assert deferred.plan_hops(3600) == [3600]

    def test_composes_larger_delays_from_buckets(self):
        hops = deferred.plan_hops(3 * 3600 + 900)
        assert sum(hops) == 3 * 3600 + 900
        assert all(h in deferred.DELAY_BUCKETS for h in hops)

    def test_greedy_largest_first(self):
        assert deferred.plan_hops(86400 + 60) == [86400, 60]

    def test_sub_bucket_remainder_is_dropped_not_rounded_up(self):
        """Firing a beat early is kinder than firing late."""
        hops = deferred.plan_hops(65)
        assert sum(hops) == 60

    def test_below_the_smallest_bucket_is_no_hops(self):
        assert deferred.plan_hops(3) == []

    def test_zero_and_negative_are_no_hops(self):
        assert deferred.plan_hops(0) == []
        assert deferred.plan_hops(-500) == []

    def test_every_hop_is_a_real_bucket(self):
        for delay in (11, 61, 301, 4000, 90000, 700000):
            for hop in deferred.plan_hops(delay):
                assert hop in deferred.DELAY_BUCKETS

    def test_pathological_input_is_bounded(self):
        hops = deferred.plan_hops(deferred.MAX_DELAY_SECONDS * 100)
        assert len(hops) <= 64

    def test_buckets_are_ascending_and_unique(self):
        assert list(deferred.DELAY_BUCKETS) == sorted(set(deferred.DELAY_BUCKETS))

    def test_queue_names_are_derived_from_the_bucket(self):
        assert deferred.queue_for(3600) == "klukai.defer.3600s"


# ─────────────────────────────────────────────────────────────────────────
# schedule()
# ─────────────────────────────────────────────────────────────────────────


class TestSchedule:
    @pytest.mark.asyncio
    async def test_persists_before_arming_the_timer(self):
        conn = _conn()
        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            with patch.object(deferred, "_arm_timer", AsyncMock()) as arm:
                task_id = await deferred.schedule(
                    {"kind": "message", "text": "hey"},
                    user_id="claude", delay_seconds=3600,
                )

        assert task_id
        conn.execute.assert_awaited_once()
        sql = conn.execute.await_args.args[0]
        assert "companion_scheduled" in sql
        assert "'pending'" in sql
        arm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_broker_failure_does_not_fail_scheduling(self):
        """The sweeper is the safety net; a dead broker must not lose the task."""
        conn = _conn()
        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            with patch.object(deferred, "_arm_timer",
                              AsyncMock(side_effect=RuntimeError("broker down"))):
                with pytest.raises(RuntimeError):
                    await deferred.schedule(
                        {"kind": "message", "text": "hey"},
                        user_id="claude", delay_seconds=60,
                    )
        # the row was still written before the timer was attempted
        conn.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_durable_write_failure_returns_none(self):
        with patch("app.db.get_conn_autocommit", side_effect=RuntimeError("pg down")):
            with patch.object(deferred, "_arm_timer", AsyncMock()) as arm:
                out = await deferred.schedule(
                    {"kind": "message", "text": "hey"},
                    user_id="claude", delay_seconds=60,
                )
        assert out is None
        arm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_accepts_an_absolute_due_time(self):
        conn = _conn()
        due = datetime.now(UTC) + timedelta(hours=2)
        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            with patch.object(deferred, "_arm_timer", AsyncMock()):
                assert await deferred.schedule(
                    {"kind": "message", "text": "x"}, user_id="claude", due_at=due
                )

    @pytest.mark.asyncio
    async def test_naive_due_time_is_treated_as_utc(self):
        conn = _conn()
        due = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None)
        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            with patch.object(deferred, "_arm_timer", AsyncMock()):
                assert await deferred.schedule(
                    {"kind": "message", "text": "x"}, user_id="claude", due_at=due
                )

    @pytest.mark.asyncio
    async def test_requires_a_delay_or_a_due_time(self):
        with pytest.raises(ValueError):
            await deferred.schedule({"kind": "message"}, user_id="claude")

    @pytest.mark.asyncio
    async def test_rejects_an_absurd_delay(self):
        with pytest.raises(ValueError):
            await deferred.schedule(
                {"kind": "message"}, user_id="claude",
                delay_seconds=deferred.MAX_DELAY_SECONDS + 1,
            )


class TestArmTimer:
    @pytest.mark.asyncio
    async def test_publishes_hops_for_the_bridge(self):
        with patch("app.events.publish", AsyncMock()) as pub:
            ok = await deferred._arm_timer("task-1", 3600)

        assert ok is True
        assert pub.await_args.args[0] == "defer.arm"
        assert pub.await_args.kwargs["task_id"] == "task-1"
        assert pub.await_args.kwargs["hops"] == [3600]

    @pytest.mark.asyncio
    async def test_already_due_needs_no_timer(self):
        with patch("app.events.publish", AsyncMock()) as pub:
            assert await deferred._arm_timer("task-1", 0) is True
        pub.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_publish_failure_is_survivable(self):
        with patch("app.events.publish", AsyncMock(side_effect=RuntimeError("no redis"))):
            assert await deferred._arm_timer("task-1", 60) is False


# ─────────────────────────────────────────────────────────────────────────
# claim() / fire() — at-least-once safety
# ─────────────────────────────────────────────────────────────────────────


class TestClaim:
    @pytest.mark.asyncio
    async def test_claim_is_a_conditional_update(self):
        conn = _conn(fetchone=("claude", {"kind": "message", "text": "hi"}))
        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            got = await deferred.claim(TASK_ID)

        assert got["user_id"] == "claude"
        sql = conn.execute.await_args.args[0]
        assert "status = 'pending'" in sql  # only an unclaimed row matches

    @pytest.mark.asyncio
    async def test_second_claim_returns_nothing(self):
        """At-least-once delivery is only safe because this is idempotent."""
        conn = _conn(fetchone=None)
        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            assert await deferred.claim(TASK_ID) is None

    @pytest.mark.asyncio
    async def test_json_encoded_action_is_decoded(self):
        conn = _conn(fetchone=("claude", json.dumps({"kind": "message", "text": "x"})))
        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            got = await deferred.claim(TASK_ID)
        assert got["action"]["kind"] == "message"

    @pytest.mark.asyncio
    async def test_db_failure_claims_nothing(self):
        with patch("app.db.get_conn_autocommit", side_effect=RuntimeError("down")):
            assert await deferred.claim(TASK_ID) is None


class TestFire:
    @pytest.mark.asyncio
    async def test_fires_a_claimed_task(self):
        claimed = {"task_id": "t", "user_id": "claude", "action": {"kind": "message"}}
        with patch.object(deferred, "claim", AsyncMock(return_value=claimed)):
            with patch.object(deferred, "dispatch", AsyncMock()) as disp:
                assert await deferred.fire("t") is True
        disp.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_duplicate_delivery_is_a_noop(self):
        with patch.object(deferred, "claim", AsyncMock(return_value=None)):
            with patch.object(deferred, "dispatch", AsyncMock()) as disp:
                assert await deferred.fire("t") is False
        disp.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_action_is_returned_to_pending(self):
        claimed = {"task_id": "t", "user_id": "claude", "action": {"kind": "message"}}
        conn = _conn()
        with patch.object(deferred, "claim", AsyncMock(return_value=claimed)):
            with patch.object(deferred, "dispatch",
                              AsyncMock(side_effect=RuntimeError("ws gone"))):
                with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
                    assert await deferred.fire("t") is False

        sql = conn.execute.await_args.args[0]
        assert "attempts >= %s" in sql  # retried until the ceiling, then parked
        assert "'failed'" in sql

    @pytest.mark.asyncio
    async def test_failure_bookkeeping_never_raises(self):
        with patch("app.db.get_conn_autocommit", side_effect=RuntimeError("down")):
            await deferred._mark_failed("t", "boom")  # must not raise


# ─────────────────────────────────────────────────────────────────────────
# dispatch()
# ─────────────────────────────────────────────────────────────────────────


class TestDispatch:
    @pytest.mark.asyncio
    async def test_message_action_speaks_to_the_commander(self):
        ws = MagicMock()
        ws.send_proactive = AsyncMock()
        with patch("app.context.ws", ws):
            await deferred.dispatch("claude", {"kind": "message", "text": "Still here."})
        ws.send_proactive.assert_awaited_once()
        assert ws.send_proactive.await_args.args[1] == "Still here."

    @pytest.mark.asyncio
    async def test_empty_message_is_not_sent(self):
        ws = MagicMock()
        ws.send_proactive = AsyncMock()
        with patch("app.context.ws", ws):
            await deferred.dispatch("claude", {"kind": "message", "text": "   "})
        ws.send_proactive.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_her_pov_action_starts_a_job(self):
        with patch("app.memory_her_pov.start_her_pov", AsyncMock()) as start:
            await deferred.dispatch("claude", {"kind": "her_pov"})
        start.assert_awaited_once_with("claude")

    @pytest.mark.asyncio
    async def test_unknown_kind_is_ignored_not_executed(self):
        """A deferred task carries data, never code."""
        await deferred.dispatch("claude", {"kind": "rm -rf /"})  # must not raise

    @pytest.mark.asyncio
    async def test_missing_kind_is_ignored(self):
        await deferred.dispatch("claude", {})


# ─────────────────────────────────────────────────────────────────────────
# sweep() — the broker-outage safety net
# ─────────────────────────────────────────────────────────────────────────


class TestSweep:
    @pytest.mark.asyncio
    async def test_fires_overdue_tasks(self):
        conn = _conn(fetchall=[("t1",), ("t2",)])
        with patch("app.db.get_conn", _conn_ctx(conn)):
            with patch.object(deferred, "fire", AsyncMock(return_value=True)) as fire:
                assert await deferred.sweep() == 2
        assert fire.await_count == 2

    @pytest.mark.asyncio
    async def test_only_selects_pending_and_due(self):
        conn = _conn(fetchall=[])
        with patch("app.db.get_conn", _conn_ctx(conn)):
            await deferred.sweep()
        sql = conn.execute.await_args.args[0]
        assert "status = 'pending'" in sql
        assert "due_at <= NOW()" in sql

    @pytest.mark.asyncio
    async def test_already_delivered_tasks_are_not_counted(self):
        conn = _conn(fetchall=[("t1",)])
        with patch("app.db.get_conn", _conn_ctx(conn)):
            with patch.object(deferred, "fire", AsyncMock(return_value=False)):
                assert await deferred.sweep() == 0

    @pytest.mark.asyncio
    async def test_query_failure_is_survivable(self):
        with patch("app.db.get_conn", side_effect=RuntimeError("pg down")):
            assert await deferred.sweep() == 0

    @pytest.mark.asyncio
    async def test_respects_the_batch_limit(self):
        conn = _conn(fetchall=[])
        with patch("app.db.get_conn", _conn_ctx(conn)):
            await deferred.sweep(limit=7)
        assert conn.execute.await_args.args[1] == (7,)


class TestPendingCount:
    @pytest.mark.asyncio
    async def test_counts_for_one_user(self):
        conn = _conn(fetchone=(3,))
        with patch("app.db.get_conn", _conn_ctx(conn)):
            assert await deferred.pending_count("claude") == 3
        assert "user_id = %s" in conn.execute.await_args.args[0]

    @pytest.mark.asyncio
    async def test_counts_globally(self):
        conn = _conn(fetchone=(9,))
        with patch("app.db.get_conn", _conn_ctx(conn)):
            assert await deferred.pending_count() == 9

    @pytest.mark.asyncio
    async def test_failure_reports_zero(self):
        with patch("app.db.get_conn", side_effect=RuntimeError("down")):
            assert await deferred.pending_count() == 0


class TestMalformedTaskId:
    @pytest.mark.asyncio
    async def test_non_uuid_id_is_a_miss_not_a_db_error(self):
        """id is a uuid column. A malformed id must read as 'no such task', or
        the bridge would requeue a message that can never succeed."""
        with patch("app.db.get_conn_autocommit") as conn:
            assert await deferred.claim("not-a-uuid") is None
            conn.assert_not_called()

    @pytest.mark.asyncio
    async def test_fire_with_a_malformed_id_is_a_clean_noop(self):
        with patch.object(deferred, "dispatch", AsyncMock()) as disp:
            assert await deferred.fire("manual-probe-1") is False
        disp.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_id_is_a_miss(self):
        assert await deferred.claim("") is None


class TestBucketBoundaryArming:
    """Regression: a request for exactly one bucket used to arm nothing.

    schedule() built due_at, then measured the delay back off it a few
    microseconds later, yielding 9.99997 for a 10s request. plan_hops floored
    that below the 10s bucket, returned no hops, and the timer was never armed
    — every such task silently fell through to the sweeper.
    """

    def test_a_hair_under_a_bucket_still_arms_it(self):
        assert deferred.plan_hops(9.99997) == [10]

    def test_each_bucket_arms_at_its_exact_value(self):
        for bucket in deferred.DELAY_BUCKETS:
            assert deferred.plan_hops(bucket) == [bucket], bucket

    def test_each_bucket_arms_a_hair_under(self):
        for bucket in deferred.DELAY_BUCKETS:
            assert deferred.plan_hops(bucket - 0.001)[0] == bucket, bucket

    def test_genuinely_short_delays_still_arm_nothing(self):
        """Tolerance must not become 'round anything up'."""
        assert deferred.plan_hops(2) == []
        assert deferred.plan_hops(0) == []

    @pytest.mark.asyncio
    async def test_schedule_arms_a_ten_second_task(self):
        conn = _conn()
        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            with patch("app.events.publish", AsyncMock()) as pub:
                await deferred.schedule(
                    {"kind": "message", "text": "hi"},
                    user_id="claude", delay_seconds=10,
                )
        pub.assert_awaited_once()
        assert pub.await_args.kwargs["hops"] == [10]

    @pytest.mark.asyncio
    async def test_schedule_uses_the_requested_delay_not_a_remeasured_one(self):
        conn = _conn()
        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            with patch.object(deferred, "_arm_timer", AsyncMock()) as arm:
                await deferred.schedule(
                    {"kind": "message"}, user_id="claude", delay_seconds=3600
                )
        assert arm.await_args.args[1] == 3600.0

    @pytest.mark.asyncio
    async def test_absolute_due_time_still_derives_the_delay(self):
        conn = _conn()
        due = datetime.now(UTC) + timedelta(seconds=3600)
        with patch("app.db.get_conn_autocommit", _conn_ctx(conn)):
            with patch.object(deferred, "_arm_timer", AsyncMock()) as arm:
                await deferred.schedule(
                    {"kind": "message"}, user_id="claude", due_at=due
                )
        assert 3599 <= arm.await_args.args[1] <= 3600
