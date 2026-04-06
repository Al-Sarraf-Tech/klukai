"""Tests for notification handler — health state machine."""

from __future__ import annotations

import unittest

from tgbot.notify import HealthState


class TestHealthState(unittest.TestCase):
    def test_initial_state_is_unknown(self):
        h = HealthState(threshold=3)
        assert h.status == "unknown"

    def test_success_transitions_to_up(self):
        h = HealthState(threshold=3)
        changed = h.record_success()
        assert h.status == "up"
        assert changed  # unknown -> up is a change

    def test_single_failure_does_not_alert(self):
        h = HealthState(threshold=3)
        h.record_success()
        changed = h.record_failure()
        assert h.status == "up"  # still up, threshold not met
        assert not changed

    def test_threshold_failures_transitions_to_down(self):
        h = HealthState(threshold=3)
        h.record_success()
        h.record_failure()
        h.record_failure()
        changed = h.record_failure()
        assert h.status == "down"
        assert changed  # up -> down

    def test_recovery_after_down(self):
        h = HealthState(threshold=3)
        h.record_success()
        for _ in range(3):
            h.record_failure()
        assert h.status == "down"
        changed = h.record_success()
        assert h.status == "up"
        assert changed  # down -> up


if __name__ == "__main__":
    unittest.main()
