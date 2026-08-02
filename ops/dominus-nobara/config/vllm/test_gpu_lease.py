from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import dominus_gpu_lease as lease


class GpuLeaseContractTests(unittest.TestCase):
    now = 2_000_000_000.0

    def document(self, **overrides: object) -> dict[str, object]:
        document: dict[str, object] = {
            "version": 1,
            "lease_id": "a" * 32,
            "token_sha256": "b" * 64,
            "workload": "comfyui",
            "issued_at_epoch_seconds": self.now - 10,
            "expires_at_epoch_seconds": self.now + 590,
            "ttl_seconds": 600,
            "state": "active",
        }
        document.update(overrides)
        return document

    def paths(self, directory: str) -> tuple[Path, Path]:
        root = Path(directory)
        return root / "lease.json", root / "ack.json"

    def test_valid_lease_and_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker, ack = self.paths(temporary)
            marker.write_text(json.dumps(self.document()), encoding="utf-8")
            active = lease.active_lease(marker, ack, self.now)
            self.assertIsNotNone(active)
            assert active is not None
            lease.acknowledge(active.lease_id, ack, self.now)
            self.assertTrue(lease.acknowledged(active.lease_id, ack))
            self.assertNotIn("b" * 64, ack.read_text(encoding="utf-8"))

    def test_expired_lease_and_ack_remain_until_gateway_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker, ack = self.paths(temporary)
            marker.write_text(
                json.dumps(
                    self.document(
                        issued_at_epoch_seconds=self.now - 601,
                        expires_at_epoch_seconds=self.now - 1,
                    )
                ),
                encoding="utf-8",
            )
            ack.write_text("stale", encoding="utf-8")
            active = lease.active_lease(marker, ack, self.now)
            self.assertIsNotNone(active)
            assert active is not None
            self.assertEqual(active.state, "active")
            self.assertTrue(marker.exists())
            self.assertTrue(ack.exists())

    def test_malformed_and_overlong_lease_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker, ack = self.paths(temporary)
            marker.write_text("not-json", encoding="utf-8")
            with self.assertRaises(lease.LeaseStateError):
                lease.active_lease(marker, ack, self.now)
            marker.write_text(
                json.dumps(
                    self.document(
                        ttl_seconds=601,
                        expires_at_epoch_seconds=self.now + 591,
                    )
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(lease.LeaseStateError, "600-second"):
                lease.active_lease(marker, ack, self.now)

    def test_future_and_mismatched_expiry_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker, ack = self.paths(temporary)
            marker.write_text(
                json.dumps(
                    self.document(
                        issued_at_epoch_seconds=self.now + 6,
                        expires_at_epoch_seconds=self.now + 606,
                    )
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(lease.LeaseStateError, "future"):
                lease.active_lease(marker, ack, self.now)
            marker.write_text(
                json.dumps(self.document(expires_at_epoch_seconds=self.now + 500)),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(lease.LeaseStateError, "does not match"):
                lease.active_lease(marker, ack, self.now)

    def test_cleanup_states_block_and_unknown_schema_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker, ack = self.paths(temporary)
            for state in ("cleaning", "cleanup_failed"):
                marker.write_text(
                    json.dumps(self.document(state=state)), encoding="utf-8"
                )
                active = lease.active_lease(marker, ack, self.now)
                self.assertIsNotNone(active)
                assert active is not None
                self.assertEqual(active.state, state)
            marker.write_text(
                json.dumps(self.document(state="released")), encoding="utf-8"
            )
            with self.assertRaisesRegex(lease.LeaseStateError, "state"):
                lease.active_lease(marker, ack, self.now)
            marker.write_text(
                json.dumps(self.document(workload="speaches")), encoding="utf-8"
            )
            with self.assertRaisesRegex(lease.LeaseStateError, "workload"):
                lease.active_lease(marker, ack, self.now)


if __name__ == "__main__":
    unittest.main()
