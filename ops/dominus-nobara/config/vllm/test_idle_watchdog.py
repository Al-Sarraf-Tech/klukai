from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).with_name("dominus-vllm-idle-watchdog.py")
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("dominus_vllm_idle_watchdog", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
watchdog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watchdog)


class IdleWatchdogContractTests(unittest.TestCase):
    boot_id = "test-boot"
    now_ns = 1_000_000_000_000

    def document(self, last_activity_ns: int | None = None) -> dict[str, object]:
        last = self.now_ns - 1_000_000_000 if last_activity_ns is None else last_activity_ns
        return {
            "version": 1,
            "boot_id": self.boot_id,
            "generation": 7,
            "last_activity_monotonic_ns": last,
            "hard_stop_monotonic_ns": last + watchdog.HARD_STOP_AFTER_NS,
            "active_connections": 0,
            "max_idle_ttl_seconds": 900,
        }

    def load(self, payload: str | None) -> int:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "activity.json"
            if payload is not None:
                path.write_text(payload, encoding="utf-8")
            return watchdog.locked_deadline(path, self.now_ns, self.boot_id)

    def test_missing_state_is_rejected(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.load(None)

    def test_malformed_state_is_rejected(self) -> None:
        with self.assertRaises(json.JSONDecodeError):
            self.load("not-json")

    def test_wrong_boot_is_rejected(self) -> None:
        document = self.document()
        document["boot_id"] = "older-boot"
        with self.assertRaisesRegex(ValueError, "another boot"):
            self.load(json.dumps(document))

    def test_wrong_ttl_is_rejected(self) -> None:
        document = self.document()
        document["max_idle_ttl_seconds"] = 901
        with self.assertRaisesRegex(ValueError, "900-second"):
            self.load(json.dumps(document))

    def test_future_activity_is_rejected(self) -> None:
        document = self.document(self.now_ns + 1)
        with self.assertRaisesRegex(ValueError, "future"):
            self.load(json.dumps(document))

    def test_expired_state_returns_expired_deadline(self) -> None:
        document = self.document(self.now_ns - watchdog.HARD_STOP_AFTER_NS - 1)
        deadline = self.load(json.dumps(document))
        self.assertLess(deadline, self.now_ns)

    def test_valid_state_returns_locked_deadline(self) -> None:
        document = self.document()
        deadline = self.load(json.dumps(document))
        self.assertEqual(
            deadline,
            int(document["last_activity_monotonic_ns"]) + watchdog.HARD_STOP_AFTER_NS,
        )

    def systemd_result(
        self,
        *,
        load_state: str = "loaded",
        active_state: str = "inactive",
        main_pid: str = "0",
        control_group: str = "/user.slice/vllm-server.service",
        returncode: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=returncode,
            stdout=(
                f"LoadState={load_state}\n"
                f"ActiveState={active_state}\n"
                f"MainPID={main_pid}\n"
                f"ControlGroup={control_group}\n"
            ),
            stderr="",
        )

    def test_backend_quiescence_accepts_only_empty_inactive_cgroup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cgroup_root = Path(directory)
            group = cgroup_root / "user.slice/vllm-server.service"
            group.mkdir(parents=True)
            (group / "cgroup.procs").write_text("", encoding="utf-8")
            with mock.patch.object(
                watchdog.subprocess,
                "run",
                return_value=self.systemd_result(),
            ):
                self.assertTrue(watchdog.backend_quiesced(cgroup_root))

            (group / "cgroup.procs").write_text("4242\n", encoding="utf-8")
            with mock.patch.object(
                watchdog.subprocess,
                "run",
                return_value=self.systemd_result(),
            ):
                self.assertFalse(watchdog.backend_quiesced(cgroup_root))

    def test_backend_quiescence_accepts_loaded_inactive_without_cgroup(self) -> None:
        with mock.patch.object(
            watchdog.subprocess,
            "run",
            return_value=self.systemd_result(control_group=""),
        ):
            self.assertTrue(watchdog.backend_quiesced(Path("/unused")))

    def test_backend_quiescence_rejects_transitional_states(self) -> None:
        for active_state in ("activating", "deactivating"):
            with self.subTest(active_state=active_state), mock.patch.object(
                watchdog.subprocess,
                "run",
                return_value=self.systemd_result(active_state=active_state),
            ):
                self.assertFalse(watchdog.backend_quiesced(Path("/unused")))

    def test_backend_quiescence_rejects_query_timeout(self) -> None:
        with mock.patch.object(
            watchdog.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="systemctl", timeout=3),
        ):
            self.assertFalse(watchdog.backend_quiesced(Path("/unused")))

    def test_backend_quiescence_rejects_unknown_or_incomplete_evidence(self) -> None:
        results = (
            self.systemd_result(returncode=1),
            self.systemd_result(load_state="not-found", control_group=""),
            self.systemd_result(active_state="unknown"),
            self.systemd_result(main_pid="99"),
            self.systemd_result(control_group="/"),
        )
        for result in results:
            with self.subTest(result=result), mock.patch.object(
                watchdog.subprocess, "run", return_value=result
            ):
                self.assertFalse(watchdog.backend_quiesced(Path("/unused")))

    def test_hard_stop_issues_kill_and_stop_and_rejects_kill_failure(self) -> None:
        with mock.patch.object(
            watchdog, "systemctl", side_effect=(1, 0)
        ) as systemctl:
            self.assertFalse(watchdog.hard_stop("test lease"))
        self.assertEqual(systemctl.call_count, 2)
        self.assertEqual(systemctl.call_args_list[0].args[0], "kill")
        self.assertEqual(systemctl.call_args_list[1].args[0], "stop")

    def test_already_inactive_lease_path_skips_kill_but_requires_recheck(self) -> None:
        with mock.patch.object(
            watchdog,
            "backend_evidence",
            return_value=watchdog.BACKEND_QUIESCED,
        ), mock.patch.object(watchdog, "hard_stop") as hard_stop:
            self.assertTrue(watchdog.prepare_backend_for_lease("test lease"))
            hard_stop.assert_not_called()

        with mock.patch.object(
            watchdog.subprocess,
            "run",
            side_effect=(
                self.systemd_result(control_group=""),
                self.systemd_result(control_group=""),
            ),
        ) as systemd_query:
            self.assertEqual(
                watchdog.backend_evidence(Path("/unused")),
                watchdog.BACKEND_QUIESCED,
            )
            self.assertTrue(watchdog.backend_quiesced(Path("/unused")))
            self.assertEqual(systemd_query.call_count, 2)

    def test_unknown_evidence_attempts_stop_but_cannot_ack(self) -> None:
        with mock.patch.object(
            watchdog,
            "backend_evidence",
            return_value=watchdog.BACKEND_UNSAFE,
        ), mock.patch.object(watchdog, "hard_stop", return_value=True) as hard_stop:
            self.assertFalse(watchdog.prepare_backend_for_lease("invalid marker"))
            hard_stop.assert_called_once_with("invalid marker")

    def test_running_backend_requires_successful_stop_and_positive_recheck(self) -> None:
        with mock.patch.object(
            watchdog,
            "backend_evidence",
            return_value=watchdog.BACKEND_RUNNING,
        ), mock.patch.object(
            watchdog, "hard_stop", return_value=False
        ), mock.patch.object(watchdog, "backend_quiesced") as recheck:
            self.assertFalse(watchdog.prepare_backend_for_lease("test lease"))
            recheck.assert_not_called()

        with mock.patch.object(
            watchdog,
            "backend_evidence",
            return_value=watchdog.BACKEND_RUNNING,
        ), mock.patch.object(
            watchdog, "hard_stop", return_value=True
        ), mock.patch.object(
            watchdog, "backend_quiesced", return_value=True
        ) as recheck:
            self.assertTrue(watchdog.prepare_backend_for_lease("test lease"))
            recheck.assert_called_once_with()

    def test_normal_idle_loop_stops_on_unknown_activity_evidence(self) -> None:
        with mock.patch.object(
            watchdog,
            "backend_evidence",
            return_value=watchdog.BACKEND_UNSAFE,
        ), mock.patch.object(watchdog, "hard_stop") as hard_stop:
            self.assertEqual(watchdog.enforce_idle_deadline(), watchdog.POLL_SECONDS)
            hard_stop.assert_called_once_with(
                "cannot obtain reliable systemd/cgroup activity evidence"
            )

    def test_normal_idle_loop_does_nothing_when_inactive_without_cgroup(self) -> None:
        with mock.patch.object(
            watchdog.subprocess,
            "run",
            return_value=self.systemd_result(control_group=""),
        ), mock.patch.object(watchdog, "hard_stop") as hard_stop:
            self.assertEqual(watchdog.enforce_idle_deadline(), watchdog.POLL_SECONDS)
            hard_stop.assert_not_called()

    def test_running_backend_stops_on_malformed_or_expired_deadline(self) -> None:
        with mock.patch.object(
            watchdog,
            "backend_evidence",
            return_value=watchdog.BACKEND_RUNNING,
        ), mock.patch.object(
            watchdog.os.path, "ismount", return_value=True
        ), mock.patch.object(
            watchdog, "locked_deadline", side_effect=ValueError("bad state")
        ), mock.patch.object(watchdog, "hard_stop") as hard_stop:
            self.assertEqual(watchdog.enforce_idle_deadline(), watchdog.POLL_SECONDS)
            self.assertIn("malformed", hard_stop.call_args.args[0])

        with mock.patch.object(
            watchdog,
            "backend_evidence",
            return_value=watchdog.BACKEND_RUNNING,
        ), mock.patch.object(
            watchdog.os.path, "ismount", return_value=True
        ), mock.patch.object(
            watchdog, "locked_deadline", return_value=99
        ), mock.patch.object(
            watchdog.time, "monotonic_ns", return_value=100
        ), mock.patch.object(watchdog, "hard_stop") as hard_stop:
            self.assertEqual(watchdog.enforce_idle_deadline(), watchdog.POLL_SECONDS)
            hard_stop.assert_called_once_with(
                "895-second idle safety deadline reached"
            )


if __name__ == "__main__":
    unittest.main()
