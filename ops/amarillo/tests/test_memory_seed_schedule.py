from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
import unittest


OPS_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = OPS_ROOT / "scripts/klukai-memory-seed-day-condition.py"
SERVICE = OPS_ROOT / "systemd/klukai-memory-archive-seed.service"
TIMER = OPS_ROOT / "systemd/klukai-memory-archive-seed.timer"
SPEC = importlib.util.spec_from_file_location("memory_seed_day_condition", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
schedule = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = schedule
SPEC.loader.exec_module(schedule)


class MemorySeedScheduleTests(unittest.TestCase):
    def test_local_epoch_day_parity_alternates_without_drift(self) -> None:
        first = datetime(2026, 8, 2, 4, 0, tzinfo=schedule.LOCAL_ZONE)
        results = [schedule.should_run(first + timedelta(days=offset)) for offset in range(4)]
        self.assertIn(results, ([True, False, True, False], [False, True, False, True]))

    def test_execution_is_rejected_outside_absolute_local_window(self) -> None:
        candidate = datetime(2026, 8, 2, 4, 0, tzinfo=schedule.LOCAL_ZONE)
        if not schedule.should_run(candidate):
            candidate += timedelta(days=1)
        self.assertTrue(schedule.should_run(candidate.replace(hour=3, minute=0)))
        self.assertTrue(schedule.should_run(candidate.replace(hour=5, minute=59)))
        self.assertFalse(schedule.should_run(candidate.replace(hour=2, minute=59)))
        self.assertFalse(schedule.should_run(candidate.replace(hour=6, minute=0)))

    def test_units_lock_daily_trigger_condition_and_no_catchup(self) -> None:
        service = SERVICE.read_text(encoding="utf-8")
        timer = TIMER.read_text(encoding="utf-8")
        self.assertIn(
            "ExecCondition=/home/jalsarraf/.local/bin/klukai-memory-seed-day-condition",
            service,
        )
        self.assertIn(
            "ExecStart=/usr/bin/docker compose exec -T companion-core "
            "python3 /app/seed_memories.py",
            service,
        )
        self.assertIn("OnCalendar=*-*-* 04:00:00 America/Chicago", timer)
        self.assertIn("Persistent=false", timer)
        self.assertIn("RandomizedDelaySec=0", timer)


if __name__ == "__main__":
    unittest.main()
