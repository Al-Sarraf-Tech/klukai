"""Tests for the live anniversary-check proactive hook."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The send gate (_can_send) reads the Commander's wall clock; 08:00 is the
# scheduled fire time and the first minute outside quiet hours.
_FIRE_TIME = datetime(2026, 5, 17, 8, 0, 0)


def _at(engine_now: datetime):
    return patch("app.proactive.engine.now_local", return_value=engine_now)


def _one_year_ago_today():
    """A 'first' whose anniversary is today, in the UTC form the table stores
    (select_anniversary_from_firsts compares against datetime.now(UTC))."""
    today = datetime.now(timezone.utc)
    return today.replace(year=today.year - 1)


class _FakeConn:
    def __init__(self, *batches):
        self._batches = list(batches)

    async def execute(self, sql, params=None):
        result = AsyncMock()
        if self._batches:
            result.fetchall = AsyncMock(return_value=self._batches.pop(0))
        else:
            result.fetchall = AsyncMock(return_value=[])
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _FakePool:
    def __init__(self, *batch_sequences):
        # Each sequence applies to one connection() call
        self._sequences = list(batch_sequences)

    def connection(self):
        seq = self._sequences.pop(0) if self._sequences else []
        return _FakeConn(*seq)


class TestAnniversaryCheck:
    @pytest.mark.asyncio
    async def test_no_active_users_no_send(self):
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()

        pool = _FakePool([[]])  # users query returns empty

        fake_ws = MagicMock()
        fake_ws.is_connected = MagicMock(return_value=True)
        fake_ws.send_proactive = AsyncMock()

        import app.context as context_mod
        orig_ws = context_mod.ws
        context_mod.ws = fake_ws
        try:
            with patch("app.db.get_pool", return_value=pool):
                await engine._anniversary_check()
        finally:
            context_mod.ws = orig_ws

        fake_ws.send_proactive.assert_not_called()

    @pytest.mark.asyncio
    async def test_anniversary_sends_when_connected(self):
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()

        pool = _FakePool(
            [[("alice",)]],                                    # users
            [[("first_message", _one_year_ago_today(), None)]],  # firsts for alice
        )

        fake_ws = MagicMock()
        fake_ws.is_connected = MagicMock(return_value=True)
        fake_ws.send_proactive = AsyncMock()

        import app.context as context_mod
        orig_ws = context_mod.ws
        context_mod.ws = fake_ws
        try:
            with _at(_FIRE_TIME), patch("app.db.get_pool", return_value=pool):
                await engine._anniversary_check()
        finally:
            context_mod.ws = orig_ws

        fake_ws.send_proactive.assert_awaited_once()
        args = fake_ws.send_proactive.await_args[0]
        assert args[0] == "alice"
        assert "year" in args[1]
        # The daily cap is the owner's scheduled-ping state: a line to another
        # Commander does not use it up.
        assert engine._proactive_count_today == 0
        assert engine._last_proactive_answered is True

    @pytest.mark.asyncio
    async def test_owners_anniversary_counts_toward_his_cap(self):
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()

        pool = _FakePool(
            [[("jalsarraf",)]],
            [[("first_message", _one_year_ago_today(), None)]],
        )
        fake_ws = MagicMock()
        fake_ws.is_connected = MagicMock(return_value=True)
        fake_ws.send_proactive = AsyncMock()
        with _at(_FIRE_TIME), patch("app.db.get_pool", return_value=pool), \
             patch("app.context.ws", fake_ws):
            await engine._anniversary_check()

        fake_ws.send_proactive.assert_awaited_once()
        # Counted against the daily cap like any proactive line, but it is the
        # opener to the 0800 greeting — it must not veto that greeting as an
        # "unanswered proactive".
        assert engine._proactive_count_today == 1
        assert engine._last_proactive_answered is True

    @pytest.mark.asyncio
    async def test_no_anniversary_no_send(self):
        """User has firsts but none match today — no send."""
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()

        # First set to some random past date that doesn't match today
        unmatched = datetime(2024, 1, 1, tzinfo=timezone.utc)
        pool = _FakePool(
            [[("alice",)]],
            [[("first_mission", unmatched, None)]],
        )

        fake_ws = MagicMock()
        fake_ws.send_proactive = AsyncMock()

        import app.context as context_mod
        orig_ws = context_mod.ws
        context_mod.ws = fake_ws
        try:
            with patch("app.db.get_pool", return_value=pool):
                await engine._anniversary_check()
        finally:
            context_mod.ws = orig_ws

        fake_ws.send_proactive.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_error_swallowed(self):
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()

        def broken():
            raise RuntimeError("db down")

        with patch("app.db.get_pool", side_effect=broken):
            # Must not raise
            await engine._anniversary_check()


class TestAnniversarySendGate:
    """The remark goes through _can_send() like every other proactive line:
    a dated occasion is no licence to ping him through a goodbye hold, a mute,
    quiet hours or the daily cap. The gate is read at fire time, so a catch-up
    replay hours later is judged by the clock it actually runs at."""

    def _engine_and_ws(self):
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()
        fake_ws = MagicMock()
        fake_ws.is_connected = MagicMock(return_value=True)
        fake_ws.send_proactive = AsyncMock()
        return engine, fake_ws

    def _pool(self):
        return _FakePool(
            [[("alice",)]],
            [[("first_message", _one_year_ago_today(), None)]],
        )

    async def _run(self, engine, fake_ws, now: datetime):
        with _at(now), \
             patch("app.db.get_pool", return_value=self._pool()), \
             patch("app.context.ws", fake_ws):
            await engine._anniversary_check()

    @pytest.mark.asyncio
    async def test_goodbye_hold_suppresses(self):
        engine, fake_ws = self._engine_and_ws()
        engine._goodbye_hold_until = _FIRE_TIME + timedelta(hours=1)  # goodnight at 0200
        await self._run(engine, fake_ws, _FIRE_TIME)
        fake_ws.send_proactive.assert_not_awaited()
        assert engine._proactive_count_today == 0

    @pytest.mark.asyncio
    async def test_mute_suppresses(self):
        engine, fake_ws = self._engine_and_ws()
        engine._muted_until = datetime(2099, 1, 1)
        await self._run(engine, fake_ws, _FIRE_TIME)
        fake_ws.send_proactive.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_daily_cap_suppresses(self):
        from app.proactive.state import MAX_PROACTIVE_PER_DAY
        engine, fake_ws = self._engine_and_ws()
        engine._proactive_count_today = MAX_PROACTIVE_PER_DAY
        await self._run(engine, fake_ws, _FIRE_TIME)
        fake_ws.send_proactive.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_late_replay_inside_quiet_hours_suppresses(self):
        """A 14h catch-up window means a restart at 21:59 still replays the
        0800 job; a replay that lands at 23:30 must stay silent."""
        engine, fake_ws = self._engine_and_ws()
        await self._run(engine, fake_ws, datetime(2026, 5, 17, 23, 30, 0))
        fake_ws.send_proactive.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_afternoon_catch_up_replay_still_delivers(self):
        """Replayed at 14:00 after a morning outage: gate passes, line goes out."""
        engine, fake_ws = self._engine_and_ws()
        await self._run(engine, fake_ws, datetime(2026, 5, 17, 14, 0, 0))
        fake_ws.send_proactive.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unanswered_morning_greeting_does_not_veto(self):
        """The 0800 greeting shares the minute and is, naturally, unanswered."""
        engine, fake_ws = self._engine_and_ws()
        engine._last_proactive_answered = False
        await self._run(engine, fake_ws, _FIRE_TIME)
        fake_ws.send_proactive.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_job_registered_on_start(self):
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()
        try:
            engine.start()
            job_ids = {j.id for j in engine._scheduler.get_jobs()}
            assert "anniversary_check" in job_ids
        finally:
            engine.stop()
