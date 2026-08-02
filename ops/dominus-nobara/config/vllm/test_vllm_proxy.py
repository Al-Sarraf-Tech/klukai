from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import dominus_gpu_lease as lease_module


SCRIPT = Path(__file__).with_name("dominus-vllm-proxy.py")
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("dominus_vllm_proxy", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


class GpuLeaseBlockTests(unittest.TestCase):
    def test_no_lease_permits_native_vllm(self) -> None:
        with mock.patch.object(proxy, "active_lease", return_value=None):
            self.assertIsNone(proxy.gpu_lease_block())

    def test_active_lease_blocks_and_names_the_workload(self) -> None:
        active = lease_module.ActiveLease(
            lease_id="a" * 32,
            workload="companion-voice",
            expires_at_epoch_seconds=1.0,
            state="active",
        )
        with mock.patch.object(proxy, "active_lease", return_value=active):
            blocked = proxy.gpu_lease_block()
        self.assertIsNotNone(blocked)
        assert blocked is not None
        self.assertEqual(blocked.code, "gpu_leased")
        self.assertIn("companion-voice", str(blocked))

    def test_invalid_marker_is_fail_closed(self) -> None:
        with mock.patch.object(
            proxy, "active_lease", side_effect=lease_module.LeaseStateError("bad")
        ):
            blocked = proxy.gpu_lease_block()
        self.assertIsNotNone(blocked)
        assert blocked is not None
        self.assertEqual(blocked.code, "gpu_lease_state_invalid")


class RestoreLockedDeadlineTests(unittest.IsolatedAsyncioTestCase):
    boot_id = "test-boot"
    now_ns = 5_000_000_000_000

    def document(self, **overrides: object) -> dict[str, object]:
        last = self.now_ns - 1_000_000_000
        document: dict[str, object] = {
            "version": 1,
            "boot_id": self.boot_id,
            "generation": 3,
            "last_activity_monotonic_ns": last,
            "hard_stop_monotonic_ns": last + proxy.HARD_STOP_AFTER_NS,
            "active_connections": 0,
            "max_idle_ttl_seconds": proxy.MAX_IDLE_TTL_SECONDS,
        }
        document.update(overrides)
        return document

    async def restore(self, payload: str) -> int:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "activity.json"
            path.write_text(payload, encoding="utf-8")
            with mock.patch.object(proxy, "STATE_PATH", path), mock.patch.object(
                proxy, "BOOT_ID", self.boot_id
            ), mock.patch.object(proxy.time, "monotonic_ns", return_value=self.now_ns):
                return proxy.restore_locked_deadline()

    async def test_wrong_boot_is_rejected(self) -> None:
        document = self.document(boot_id="a-different-boot")
        with self.assertRaisesRegex(ValueError, "another boot"):
            await self.restore(json.dumps(document))

    async def test_wrong_ttl_is_rejected(self) -> None:
        document = self.document(max_idle_ttl_seconds=901)
        with self.assertRaisesRegex(ValueError, "locked TTL"):
            await self.restore(json.dumps(document))

    async def test_future_activity_is_rejected(self) -> None:
        document = self.document(last_activity_monotonic_ns=self.now_ns + 1)
        # hard_stop_monotonic_ns must still satisfy the fixed-offset check.
        document["hard_stop_monotonic_ns"] = (
            self.now_ns + 1 + proxy.HARD_STOP_AFTER_NS
        )
        with self.assertRaisesRegex(ValueError, "future"):
            await self.restore(json.dumps(document))

    async def test_mismatched_deadline_offset_is_rejected(self) -> None:
        document = self.document()
        document["hard_stop_monotonic_ns"] = int(
            document["last_activity_monotonic_ns"]
        ) + 1
        with self.assertRaisesRegex(ValueError, "895-second"):
            await self.restore(json.dumps(document))

    async def test_negative_generation_is_rejected(self) -> None:
        document = self.document(generation=-1)
        with self.assertRaisesRegex(ValueError, "generation"):
            await self.restore(json.dumps(document))

    async def test_valid_state_restores_the_locked_deadline_and_generation(
        self,
    ) -> None:
        document = self.document()
        deadline = await self.restore(json.dumps(document))
        self.assertEqual(
            deadline,
            int(document["last_activity_monotonic_ns"]) + proxy.HARD_STOP_AFTER_NS,
        )
        self.assertEqual(proxy._generation, document["generation"])
        self.assertEqual(proxy._deadline_ns, deadline)
        if proxy._deadline_handle is not None:
            proxy._deadline_handle.cancel()


class EnsureBackendStartedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        marker_patch = mock.patch.object(
            proxy, "GAME_MARKER", Path(self._tmpdir.name) / "game-active"
        )
        marker_patch.start()
        self.addCleanup(marker_patch.stop)

    async def test_game_active_blocks_without_touching_the_backend(self) -> None:
        proxy.GAME_MARKER.touch()
        with mock.patch.object(proxy, "_backend_active") as backend_active:
            with self.assertRaises(proxy.ServiceUnavailable) as caught:
                await proxy.ensure_backend_started()
            self.assertEqual(caught.exception.code, "game_active")
            backend_active.assert_not_called()

    async def test_active_gpu_lease_stops_a_running_backend_and_blocks(self) -> None:
        with mock.patch.object(
            proxy,
            "gpu_lease_block",
            return_value=proxy.ServiceUnavailable("gpu_leased", "leased to comfyui"),
        ), mock.patch.object(
            proxy, "_backend_active", return_value=True
        ), mock.patch.object(
            proxy, "_hard_stop_backend", return_value=None
        ) as hard_stop:
            with self.assertRaises(proxy.ServiceUnavailable) as caught:
                await proxy.ensure_backend_started()
            self.assertEqual(caught.exception.code, "gpu_leased")
            hard_stop.assert_called_once()

    async def test_already_reachable_backend_returns_without_starting(self) -> None:
        with mock.patch.object(
            proxy, "gpu_lease_block", return_value=None
        ), mock.patch.object(
            proxy, "_backend_reachable", return_value=True
        ), mock.patch.object(proxy, "_run_systemctl") as run_systemctl:
            await proxy.ensure_backend_started()
            run_systemctl.assert_not_called()

    async def test_vram_preflight_rejection_blocks_the_cold_start(self) -> None:
        with mock.patch.object(
            proxy, "gpu_lease_block", return_value=None
        ), mock.patch.object(
            proxy, "_backend_reachable", return_value=False
        ), mock.patch.object(
            proxy, "_run_vram_preflight", return_value=False
        ), mock.patch.object(proxy, "_run_systemctl") as run_systemctl:
            with self.assertRaises(proxy.ServiceUnavailable) as caught:
                await proxy.ensure_backend_started()
            self.assertEqual(caught.exception.code, "insufficient_vram")
            run_systemctl.assert_not_called()

    async def test_systemctl_start_failure_is_reported(self) -> None:
        with mock.patch.object(
            proxy, "gpu_lease_block", return_value=None
        ), mock.patch.object(
            proxy, "_backend_reachable", return_value=False
        ), mock.patch.object(
            proxy, "_run_vram_preflight", return_value=True
        ), mock.patch.object(proxy, "_run_systemctl", return_value=1):
            with self.assertRaises(proxy.ServiceUnavailable) as caught:
                await proxy.ensure_backend_started()
            self.assertEqual(caught.exception.code, "start_failed")

    async def test_successful_cold_start_records_activity_once_reachable(
        self,
    ) -> None:
        reachable_calls = {"count": 0}

        def reachable() -> bool:
            reachable_calls["count"] += 1
            return reachable_calls["count"] > 1

        with mock.patch.object(
            proxy, "gpu_lease_block", return_value=None
        ), mock.patch.object(
            proxy, "_backend_reachable", side_effect=reachable
        ), mock.patch.object(
            proxy, "_run_vram_preflight", return_value=True
        ), mock.patch.object(
            proxy, "_run_systemctl", return_value=0
        ), mock.patch.object(
            proxy, "_backend_active", return_value=True
        ), mock.patch.object(
            proxy, "record_activity"
        ) as record_activity, mock.patch.object(
            proxy.asyncio, "sleep", return_value=None
        ):
            await proxy.ensure_backend_started()
        record_activity.assert_called_once()


if __name__ == "__main__":
    unittest.main()
