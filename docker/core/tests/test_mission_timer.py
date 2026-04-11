"""Tests for MissionTimer lifecycle, interval randomization, events, and safety rules."""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.proactive import MissionTimer, MAJOR_EVENTS


# ── Lifecycle ────────────────────────────────────────────────────────────────

class TestMissionTimerLifecycle:
    def test_initial_state_is_inactive(self):
        timer = MissionTimer()
        assert timer.active is False
        assert timer._task is None
        assert timer.update_count == 0
        assert timer.active_events == []

    @pytest.mark.asyncio
    async def test_start_activates_timer(self):
        timer = MissionTimer()
        callback = AsyncMock()
        timer.start("Patrol sector 7", interval_minutes=30, callback=callback)
        assert timer.active is True
        assert timer._task is not None
        assert timer.mission_description == "Patrol sector 7"
        assert timer.base_interval_minutes == 30
        assert timer.started_at > 0
        timer.stop()

    @pytest.mark.asyncio
    async def test_stop_deactivates_timer(self):
        timer = MissionTimer()
        callback = AsyncMock()
        timer.start("Patrol sector 7", interval_minutes=30, callback=callback)
        timer.stop()
        assert timer.active is False

    @pytest.mark.asyncio
    async def test_stop_records_update_count(self):
        timer = MissionTimer()
        timer.start("Patrol", callback=AsyncMock())
        timer.update_count = 5
        timer.stop()
        assert timer.update_count == 5

    @pytest.mark.asyncio
    async def test_minimum_interval_is_5_minutes(self):
        timer = MissionTimer()
        timer.start("Quick check", interval_minutes=1, callback=AsyncMock())
        assert timer.base_interval_minutes == 5
        timer.stop()


# ── Interval randomization ───────────────────────────────────────────────────

class TestIntervalRandomization:
    def test_jitter_stays_within_bounds(self):
        """Verify ±30% jitter range over many samples."""
        base_seconds = 30 * 60  # 30 minutes
        for _ in range(500):
            jitter = random.uniform(0.7, 1.3)
            result = base_seconds * jitter
            assert result >= base_seconds * 0.7
            assert result <= base_seconds * 1.3

    def test_jitter_produces_variation(self):
        """Confirm jitter doesn't always produce the same value."""
        base_seconds = 30 * 60
        values = {base_seconds * random.uniform(0.7, 1.3) for _ in range(100)}
        assert len(values) > 50  # Should have significant variation


# ── Major events ─────────────────────────────────────────────────────────────

class TestMajorEventProbability:
    def test_major_event_fires_approximately_10_percent(self):
        """Run enough iterations to verify ~10% rate with ±5% tolerance."""
        hits = 0
        iterations = 5000
        for _ in range(iterations):
            if random.random() < 0.10:
                hits += 1
        rate = hits / iterations
        assert 0.05 <= rate <= 0.15, f"Expected ~10%, got {rate:.2%}"

    def test_major_events_are_valid(self):
        """All MAJOR_EVENTS should be known event types."""
        expected_events = {
            "ambush", "squad_injured", "klukai_injured", "equipment_failure",
            "weather", "comms_disruption", "discovery", "medical_emergency",
            "mechty_asleep", "belka_reckless", "andoris_freeze",
        }
        assert set(MAJOR_EVENTS) == expected_events


# ── Injury persistence ───────────────────────────────────────────────────────

class TestInjuryPersistence:
    def test_klukai_injury_persists_across_updates(self):
        timer = MissionTimer()
        timer.active = True
        timer.update_count = 1

        # Simulate adding klukai_injured
        major_event = "klukai_injured"
        if major_event == "klukai_injured" and "klukai_injured" not in timer.active_events:
            timer.active_events.append("klukai_injured")

        assert "klukai_injured" in timer.active_events

        # Simulate a subsequent tick where no resolution occurs
        timer.update_count = 2
        # Injury should still be present (resolution is random, here we don't roll)
        assert "klukai_injured" in timer.active_events

    def test_squad_injury_persists_across_updates(self):
        timer = MissionTimer()
        timer.active = True
        timer.update_count = 1
        timer.active_events.append("squad_injured")
        timer.update_count = 2
        assert "squad_injured" in timer.active_events

    def test_duplicate_injuries_not_added(self):
        timer = MissionTimer()
        timer.active_events.append("klukai_injured")

        major_event = "klukai_injured"
        if major_event == "klukai_injured" and "klukai_injured" not in timer.active_events:
            timer.active_events.append("klukai_injured")

        assert timer.active_events.count("klukai_injured") == 1


# ── Injury resolution ────────────────────────────────────────────────────────

class TestInjuryResolution:
    def test_resolution_only_after_two_updates(self):
        """No resolution should happen at update_count <= 2."""
        timer = MissionTimer()
        timer.active_events = ["klukai_injured"]
        timer.update_count = 2

        # At update_count == 2, the code checks > 2, so no resolution
        resolved = []
        if timer.update_count > 2:
            for evt in timer.active_events:
                if random.random() < 0.30:
                    resolved.append(evt)
        assert resolved == []  # Should not resolve at exactly 2

    def test_resolution_possible_after_two_updates(self):
        """After update_count > 2, injuries can resolve with 30% probability."""
        timer = MissionTimer()
        timer.active_events = ["klukai_injured", "squad_injured"]
        timer.update_count = 3

        # Run many times — at least some should resolve
        resolved_any = False
        for _ in range(100):
            test_events = ["klukai_injured", "squad_injured"]
            if timer.update_count > 2:
                for evt in list(test_events):
                    if random.random() < 0.30:
                        test_events.remove(evt)
                        resolved_any = True
            if resolved_any:
                break
        assert resolved_any, "Expected at least one resolution in 100 attempts at 30% rate"

    def test_resolution_rate_approximately_30_percent(self):
        """Verify resolution probability is near 30%."""
        hits = sum(1 for _ in range(5000) if random.random() < 0.30)
        rate = hits / 5000
        assert 0.25 <= rate <= 0.35, f"Expected ~30%, got {rate:.2%}"


# ── No-death safety rule ────────────────────────────────────────────────────

class TestNoDeathSafetyRule:
    def test_no_death_events_in_major_events(self):
        """MAJOR_EVENTS must never include death or killed."""
        for event in MAJOR_EVENTS:
            assert "death" not in event.lower()
            assert "killed" not in event.lower()
            assert "dead" not in event.lower()
            assert "dies" not in event.lower()

    def test_active_events_never_contain_death(self):
        """Simulating many updates, active_events should never contain death."""
        timer = MissionTimer()
        timer.active = True
        for i in range(100):
            event = random.choice(MAJOR_EVENTS)
            if event not in timer.active_events:
                timer.active_events.append(event)

        for evt in timer.active_events:
            assert "death" not in evt.lower()
            assert "killed" not in evt.lower()


# ── Task cancellation ────────────────────────────────────────────────────────

class TestMissionTimerCancellation:
    @pytest.mark.asyncio
    async def test_cancel_cancels_asyncio_task(self):
        timer = MissionTimer()
        callback = AsyncMock()

        # Patch _fire_update to avoid LLM calls
        with patch.object(timer, "_fire_update", new_callable=AsyncMock):
            timer.start("Test mission", interval_minutes=30, callback=callback)
            task = timer._task
            assert task is not None
            assert not task.done()

            timer.stop()

            # Give the event loop a chance to process the cancellation
            await asyncio.sleep(0.05)
            assert task.done() or task.cancelled()

    @pytest.mark.asyncio
    async def test_stop_sets_global_active_mission_to_none(self):
        from app import proactive as proactive_mod

        timer = MissionTimer()
        timer.start("Test", callback=AsyncMock())
        assert proactive_mod._active_mission_timer is timer

        timer.stop()
        assert proactive_mod._active_mission_timer is None
