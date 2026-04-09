"""Tests for idle auto-unload: user activity tracking, idle detection, keepalive behavior."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.llm_router as llm_router_mod
from app.llm_router import (
    IDLE_TIMEOUT,
    LLMRouter,
    _is_user_idle,
    mark_user_active,
)


@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset module-level globals before each test."""
    original = llm_router_mod._last_user_message
    llm_router_mod._last_user_message = 0.0
    yield
    llm_router_mod._last_user_message = original


# ── mark_user_active ─────────────────────────────────────────────────────────

class TestMarkUserActive:
    def test_updates_timestamp(self):
        assert llm_router_mod._last_user_message == 0.0
        mark_user_active()
        assert llm_router_mod._last_user_message > 0.0

    def test_timestamp_increases_on_subsequent_calls(self):
        mark_user_active()
        first = llm_router_mod._last_user_message
        # Nudge monotonic forward by a tiny bit
        mark_user_active()
        second = llm_router_mod._last_user_message
        assert second >= first


# ── _is_user_idle ────────────────────────────────────────────────────────────

class TestIsUserIdle:
    def test_returns_false_on_fresh_startup(self):
        """Before any message, _is_user_idle returns False (don't unload on fresh start)."""
        llm_router_mod._last_user_message = 0.0
        assert _is_user_idle() is False

    def test_returns_false_before_timeout(self):
        """Recent activity means not idle."""
        llm_router_mod._last_user_message = time.monotonic()
        assert _is_user_idle() is False

    def test_returns_true_after_timeout(self):
        """No activity for > IDLE_TIMEOUT means idle."""
        llm_router_mod._last_user_message = time.monotonic() - IDLE_TIMEOUT - 1
        assert _is_user_idle() is True

    def test_returns_false_just_before_timeout(self):
        """Just before IDLE_TIMEOUT, the > check means not yet idle."""
        llm_router_mod._last_user_message = time.monotonic() - IDLE_TIMEOUT + 5
        assert _is_user_idle() is False


# ── Keepalive behavior ───────────────────────────────────────────────────────

class TestKeepaliveIdleBehavior:
    @pytest.mark.asyncio
    async def test_keepalive_skips_when_idle_no_mission(self):
        """When user is idle and no mission is active, keepalive should skip."""
        r = LLMRouter()
        r._lmstudio_available = True
        r._http = AsyncMock()

        # Make user idle
        llm_router_mod._last_user_message = time.monotonic() - IDLE_TIMEOUT - 100

        with patch("app.llm_router.lm_gate_busy", return_value=False), \
             patch("app.proactive.has_active_mission", return_value=False):
            await r.keepalive()

        # HTTP post should NOT have been called (keepalive skipped)
        r._http.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_keepalive_runs_when_idle_but_mission_active(self):
        """When user is idle but a mission timer is running, keepalive should still fire."""
        r = LLMRouter()
        r._lmstudio_available = True
        r._http = AsyncMock()

        # Make user idle
        llm_router_mod._last_user_message = time.monotonic() - IDLE_TIMEOUT - 100

        # Mock the HTTP response for keepalive ping
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        r._http.post = AsyncMock(return_value=fake_resp)

        with patch("app.llm_router.lm_gate_busy", return_value=False), \
             patch("app.proactive.has_active_mission", return_value=True), \
             patch("app.llm_router.get_lm_gate") as mock_gate, \
             patch("app.llm_router.model_needs_keepalive", return_value=True):
            # Mock the gate as an async context manager
            gate_instance = AsyncMock()
            gate_instance.__aenter__ = AsyncMock(return_value=None)
            gate_instance.__aexit__ = AsyncMock(return_value=None)
            mock_gate.return_value = gate_instance

            await r.keepalive()

        # HTTP post SHOULD have been called (mission keeps models warm)
        assert r._http.post.called

    @pytest.mark.asyncio
    async def test_keepalive_skips_when_gate_busy(self):
        """If a real request is in-flight, keepalive should skip entirely."""
        r = LLMRouter()
        r._lmstudio_available = True
        r._http = AsyncMock()

        mark_user_active()  # User is active

        with patch("app.llm_router.lm_gate_busy", return_value=True):
            await r.keepalive()

        r._http.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_keepalive_skips_when_lmstudio_unavailable(self):
        """If LM Studio is down and re-check fails, keepalive should skip."""
        r = LLMRouter()
        r._lmstudio_available = False
        r._lmstudio_last_check = time.monotonic()  # recent check, won't re-check
        r._http = AsyncMock()

        with patch("app.llm_router.lm_gate_busy", return_value=False):
            await r.keepalive()

        r._http.post.assert_not_called()


# ── IDLE_TIMEOUT constant ───────────────────────────────────────────────────

class TestIdleTimeoutConstant:
    def test_idle_timeout_is_two_hours(self):
        assert IDLE_TIMEOUT == 2 * 3600

    def test_idle_timeout_is_positive(self):
        assert IDLE_TIMEOUT > 0
