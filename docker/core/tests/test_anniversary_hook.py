"""Tests for the live anniversary-check proactive hook."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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

        # Compute today's date in the same form the table stores
        today = datetime.now(timezone.utc)
        one_year_ago = today.replace(year=today.year - 1)

        pool = _FakePool(
            [[("alice",)]],                                    # users
            [[("first_message", one_year_ago, None)]],         # firsts for alice
        )

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

        fake_ws.send_proactive.assert_awaited_once()
        args = fake_ws.send_proactive.await_args[0]
        assert args[0] == "alice"
        assert "year" in args[1]

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
