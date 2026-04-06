"""Tests for Claude Code agent session management."""

from __future__ import annotations

import time
import unittest

from tgbot.agent import AgentSession


class TestAgentSession(unittest.TestCase):
    def test_new_session_has_no_id(self):
        s = AgentSession()
        assert s.session_id is None
        assert not s.is_active

    def test_session_becomes_active(self):
        s = AgentSession()
        s.session_id = "abc-123"
        s.last_activity = time.time()
        assert s.is_active

    def test_session_expires_after_idle(self):
        s = AgentSession(idle_timeout=1)
        s.session_id = "abc-123"
        s.last_activity = time.time() - 2
        assert not s.is_active

    def test_recycle_clears_session(self):
        s = AgentSession()
        s.session_id = "abc-123"
        s.last_activity = time.time()
        s.recycle()
        assert s.session_id is None


if __name__ == "__main__":
    unittest.main()
