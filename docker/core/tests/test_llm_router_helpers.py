"""Tests for app.llm_router module-level helpers (lm_gate, keepalive, idle, signals)."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from app import llm_router


class TestLmGate:
    def test_get_lm_gate_creates_on_first_call(self):
        llm_router._lm_gate = None
        gate = llm_router.get_lm_gate()
        assert gate is not None
        # Second call returns same instance
        assert llm_router.get_lm_gate() is gate

    def test_lm_gate_busy_false_when_not_locked(self):
        llm_router._lm_gate = None
        llm_router.get_lm_gate()
        assert llm_router.lm_gate_busy() is False

    @pytest.mark.asyncio
    async def test_lm_gate_busy_true_when_locked(self):
        llm_router._lm_gate = None
        gate = llm_router.get_lm_gate()
        async with gate:
            assert llm_router.lm_gate_busy() is True
        assert llm_router.lm_gate_busy() is False

    def test_lm_gate_busy_when_gate_never_created(self):
        llm_router._lm_gate = None
        assert llm_router.lm_gate_busy() is False


class TestSeedingFlag:
    def setup_method(self):
        llm_router._seeding_active = False

    def teardown_method(self):
        llm_router._seeding_active = False

    def test_set_seeding_active_true(self):
        llm_router.set_seeding_active(True)
        assert llm_router._seeding_active is True

    def test_set_seeding_active_false(self):
        llm_router._seeding_active = True
        llm_router.set_seeding_active(False)
        assert llm_router._seeding_active is False


class TestEarlyAmWindow:
    def test_function_callable_returns_bool(self):
        # Hard to patch datetime due to local-import; just verify shape.
        result = llm_router._is_early_am_window()
        assert isinstance(result, bool)


class TestUserIdleTracking:
    def setup_method(self):
        llm_router._last_user_message = 0.0

    def teardown_method(self):
        llm_router._last_user_message = 0.0

    def test_not_idle_initially_on_fresh_start(self):
        # Never messaged yet — don't unload on fresh startup
        llm_router._last_user_message = 0.0
        assert llm_router._is_user_idle() is False

    def test_mark_user_active_updates_timestamp(self):
        llm_router._last_user_message = 0.0
        llm_router.mark_user_active()
        assert llm_router._last_user_message > 0

    def test_not_idle_after_recent_message(self):
        llm_router._last_user_message = time.monotonic()
        assert llm_router._is_user_idle() is False

    def test_idle_after_long_silence(self):
        # 3 hours ago — idle threshold is 2h
        llm_router._last_user_message = time.monotonic() - (3 * 3600)
        assert llm_router._is_user_idle() is True


class TestModelKeepalive:
    def setup_method(self):
        llm_router._model_last_used = {}

    def teardown_method(self):
        llm_router._model_last_used = {}

    def test_mark_model_used_records_time(self):
        llm_router.mark_model_used("dolphin")
        assert "dolphin" in llm_router._model_last_used
        assert llm_router._model_last_used["dolphin"] > 0

    def test_unused_model_needs_keepalive(self):
        # Never seen — definitely needs keepalive
        assert llm_router.model_needs_keepalive("never-used-model") is True

    def test_recent_use_no_keepalive(self):
        llm_router.mark_model_used("dolphin")
        # Just used → doesn't need keepalive
        assert llm_router.model_needs_keepalive("dolphin") is False

    def test_old_use_needs_keepalive(self):
        # Last used 25 min ago — over the keepalive interval
        llm_router._model_last_used["dolphin"] = time.monotonic() - (30 * 60)
        assert llm_router.model_needs_keepalive("dolphin") is True


class TestAgentSignals:
    def test_signals_list_present(self):
        assert isinstance(llm_router.AGENT_SIGNALS, list)
        assert len(llm_router.AGENT_SIGNALS) > 5

    def test_signals_include_common_intents(self):
        signals = [s.lower() for s in llm_router.AGENT_SIGNALS]
        # At least one of these should be present
        assert any(s in signals for s in ["search", "look up", "find out", "today's"])


class TestModelAliases:
    def test_local_casual_defined(self):
        assert llm_router.LOCAL_CASUAL
        assert isinstance(llm_router.LOCAL_CASUAL, str)

    def test_cloud_fallback_is_haiku(self):
        assert "haiku" in llm_router.CLOUD_FALLBACK.lower()
