"""Tests for the weekly reflection scheduled job in ProactiveEngine."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _FakeRows:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, *batches):
        self._batches = list(batches)

    async def execute(self, sql, params=None):
        if not self._batches:
            return _FakeRows([])
        return _FakeRows(self._batches.pop(0))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _FakePool:
    def __init__(self, *batch_sequences):
        # Each batch_sequences entry is the list of batches for one connection() call
        self._sequences = list(batch_sequences)

    def connection(self):
        batches = self._sequences.pop(0) if self._sequences else []
        return _FakeConn(*batches)


class TestWeeklyReflection:
    @pytest.mark.asyncio
    async def test_skips_when_no_active_users(self):
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()

        # First (and only) query returns empty user list
        pool = _FakePool([[]])

        with patch("app.db.get_pool", return_value=pool):
            await engine._weekly_reflection()
        # No exception, no LLM called

    @pytest.mark.asyncio
    async def test_skips_user_with_too_few_messages(self):
        """Users with < 10 messages in the window get skipped (not enough to summarize)."""
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()

        # First connection: users query; then per-user message query (only 5 rows)
        pool = _FakePool(
            [[("alice",)]],       # connection 1: users
            [[("user", "hi")] * 5],  # connection 2: 5 messages (< 10)
        )

        fake_store = AsyncMock()
        import app.context as context_mod
        orig_memory = context_mod.memory
        context_mod.memory = MagicMock()
        context_mod.memory.store_episode = fake_store
        try:
            with patch("app.db.get_pool", return_value=pool):
                await engine._weekly_reflection()
        finally:
            context_mod.memory = orig_memory

        fake_store.assert_not_called()

    @pytest.mark.asyncio
    async def test_writes_reflection_for_active_user(self):
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()

        messages = [("user", "hello")] * 15
        pool = _FakePool(
            [[("alice",)]],
            [messages],
        )

        fake_llm_response = {
            "choices": [{"message": {"content":
                "A long reflective journal entry that describes the past week "
                "with detail and honesty. Plenty of content to pass the 50-char "
                "sanity threshold so this gets saved as a real reflection."
            }}]
        }

        fake_memory = MagicMock()
        fake_memory.store_episode = AsyncMock(return_value="ep-id")

        fake_router = MagicMock()
        fake_router.complete_local = AsyncMock(return_value=fake_llm_response)

        import app.context as context_mod
        orig_memory, orig_router = context_mod.memory, context_mod.router
        context_mod.memory = fake_memory
        context_mod.router = fake_router
        try:
            with patch("app.db.get_pool", return_value=pool), \
                 patch("app.personality.load_personality", return_value={"user_title": "Commander"}), \
                 patch("app.personality.build_character_preamble", return_value="You are Klukai."):
                await engine._weekly_reflection()
        finally:
            context_mod.memory = orig_memory
            context_mod.router = orig_router

        assert fake_memory.store_episode.called
        kwargs = fake_memory.store_episode.call_args.kwargs
        assert kwargs["conversation_id"] == "weekly-reflection"
        assert kwargs["importance"] == 8
        assert kwargs["user_id"] == "alice"

    @pytest.mark.asyncio
    async def test_handles_llm_failure_gracefully(self):
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()

        messages = [("user", "hello")] * 15
        pool = _FakePool(
            [[("alice",)]],
            [messages],
        )

        fake_memory = MagicMock()
        fake_memory.store_episode = AsyncMock(return_value="ep-id")

        fake_router = MagicMock()
        fake_router.complete_local = AsyncMock(side_effect=RuntimeError("LLM down"))

        import app.context as context_mod
        orig_memory, orig_router = context_mod.memory, context_mod.router
        context_mod.memory = fake_memory
        context_mod.router = fake_router
        try:
            with patch("app.db.get_pool", return_value=pool), \
                 patch("app.personality.load_personality", return_value={"user_title": "Commander"}), \
                 patch("app.personality.build_character_preamble", return_value="You are Klukai."):
                await engine._weekly_reflection()
        finally:
            context_mod.memory = orig_memory
            context_mod.router = orig_router

        fake_memory.store_episode.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_short_reflection_output(self):
        """Reflections shorter than 50 chars are rejected (LLM likely errored)."""
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()

        messages = [("user", "hello")] * 15
        pool = _FakePool(
            [[("alice",)]],
            [messages],
        )

        fake_llm_response = {"choices": [{"message": {"content": "ok"}}]}

        fake_memory = MagicMock()
        fake_memory.store_episode = AsyncMock()

        fake_router = MagicMock()
        fake_router.complete_local = AsyncMock(return_value=fake_llm_response)

        import app.context as context_mod
        orig_memory, orig_router = context_mod.memory, context_mod.router
        context_mod.memory = fake_memory
        context_mod.router = fake_router
        try:
            with patch("app.db.get_pool", return_value=pool), \
                 patch("app.personality.load_personality", return_value={"user_title": "Commander"}), \
                 patch("app.personality.build_character_preamble", return_value="You are Klukai."):
                await engine._weekly_reflection()
        finally:
            context_mod.memory = orig_memory
            context_mod.router = orig_router

        fake_memory.store_episode.assert_not_called()


class TestSchedulerRegistration:
    @pytest.mark.asyncio
    async def test_weekly_reflection_job_registered_on_start(self):
        """After engine.start(), the weekly_reflection job should be registered."""
        from app.proactive import ProactiveEngine

        engine = ProactiveEngine()
        try:
            engine.start()
            job_ids = {job.id for job in engine._scheduler.get_jobs()}
            assert "weekly_reflection" in job_ids
            assert "daily_reset" in job_ids  # sanity: other existing jobs still registered
        finally:
            engine.stop()
