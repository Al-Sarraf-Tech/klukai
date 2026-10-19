"""Old CI records move off the guest to amarillo, and only once verified."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout


REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ops/amarillo/scripts/klukai-fetch-ci-artifacts.py"
SPEC = importlib.util.spec_from_file_location(
    "klukai_fetch_ci_artifacts_archive", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
relay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = relay
SPEC.loader.exec_module(relay)

NOW = 1_800_000_000
HOUR = 3600


def rev(n: int) -> str:
    return f"{n:040x}"


class FakeGuest:
    """The guest's records, its outbox and the commands the archive runs."""

    def __init__(self, records: dict[str, float], corrupt: bool = False) -> None:
        self.records = dict(records)
        self.outbox: dict[str, bytes] = {}
        self.corrupt = corrupt
        self.commands: list[list[str]] = []

    def run(self, argv: list[str], timeout: float = 120) -> str:
        self.commands.append(argv)
        if argv[:3] == ["/bin/sh", "-c", relay.LIST_RECORDS]:
            assert argv[4] == relay.ARTIFACTS
            rows = [f"{stamp} {record}" for record, stamp in self.records.items()]
            return "\n".join([str(NOW), *rows, ""])
        if argv[:3] == ["/bin/sh", "-c", relay.PACK_RECORD]:
            outbox, root, packed, record = argv[4:]
            assert (outbox, root) == (relay.OUTBOX, relay.ARTIFACTS)
            assert record in self.records
            body = f"tar of {record}".encode()
            self.outbox[packed] = body
            return f"{hashlib.sha256(body).hexdigest()}  {packed}\n"
        if argv[:3] == ["/bin/rm", "-f", "--"]:
            self.outbox.pop(argv[3], None)
            return ""
        if argv[:3] == ["/bin/rm", "-rf", "--"]:
            record = argv[3].removeprefix(relay.ARTIFACTS + "/")
            del self.records[record]
            return ""
        raise AssertionError(argv)

    def read(self, path: str, dest: Path) -> None:
        body = self.outbox[path]
        dest.write_bytes(body + b"!" if self.corrupt else body)


class CandidateTests(unittest.TestCase):
    def test_newest_records_and_busy_ones_stay_on_the_guest(self) -> None:
        old = NOW - 10 * HOUR
        records = {rev(n): old + n for n in range(1, 6)}  # rev(5) is newest
        records[rev(9)] = NOW - HOUR  # newest overall, still settling
        records["nightly/2026-10-17T01-30Z-c6d02f0"] = old
        records["nightly/2026-10-18T01-30Z-c6d02f0"] = old + 1
        records["nightly/2026-10-19T12-20Z-8ff19de"] = old + 2
        records["toolchain-provenance-b838d34bba223857"] = old
        records["nightly"] = old
        listing = "\n".join([str(NOW), *(f"{t} {r}" for r, t in records.items())])
        relayed = frozenset(
            {
                "2026-10-17T01-30Z-c6d02f0",
                "2026-10-18T01-30Z-c6d02f0",
                "2026-10-19T12-20Z-8ff19de",
            }
        )
        self.assertEqual(
            relay.archive_candidates(listing, relayed),
            sorted([rev(1), rev(2), rev(3), "nightly/2026-10-17T01-30Z-c6d02f0"]),
        )

    def test_a_night_waits_until_its_summary_has_been_relayed(self) -> None:
        old = NOW - 10 * HOUR
        nights = [f"nightly/2026-10-1{d}T01-30Z-c6d02f0" for d in range(4, 8)]
        listing = "\n".join(
            [str(NOW), *(f"{old + i} {n}" for i, n in enumerate(nights))]
        )
        self.assertEqual(relay.archive_candidates(listing), [])
        relayed = frozenset(n.split("/")[1] for n in nights[:1])
        self.assertEqual(relay.archive_candidates(listing, relayed), nights[:1])

    def test_a_kept_slot_is_not_freed_by_a_busy_record(self) -> None:
        records = {
            rev(1): NOW - 10 * HOUR,
            rev(2): NOW - HOUR,
            rev(3): NOW - HOUR,
            rev(4): NOW - HOUR,
        }
        listing = "\n".join([str(NOW), *(f"{t} {r}" for r, t in records.items())])
        self.assertEqual(relay.archive_candidates(listing), [rev(1)])

    def test_unreadable_lines_are_skipped(self) -> None:
        listing = f"{NOW}\n {rev(1)}\nnot-a-number {rev(2)}\n"
        self.assertEqual(relay.archive_candidates(listing), [])


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def guest(self, corrupt: bool = False) -> FakeGuest:
        old = NOW - 10 * HOUR
        return FakeGuest({rev(n): old + n for n in range(1, 6)}, corrupt=corrupt)

    def test_verified_copies_land_and_only_then_leave_the_guest(self) -> None:
        guest = self.guest()
        result = relay.archive(guest, self.dest, limit=5)
        self.assertEqual([m["record"] for m in result["archived"]], [rev(1), rev(2)])
        self.assertEqual(
            (self.dest / f"{rev(1)}.tar").read_bytes(), f"tar of {rev(1)}".encode()
        )
        self.assertEqual(sorted(guest.records), [rev(3), rev(4), rev(5)])
        self.assertEqual(guest.outbox, {})
        self.assertEqual(
            sorted(p.name for p in self.dest.iterdir()),
            [f"{rev(1)}.tar", f"{rev(2)}.tar"],
        )
        pack = next(c for c in guest.commands if c[2] == relay.PACK_RECORD)
        self.assertIn("--exclude='*/release/core-image.tar'", relay.PACK_RECORD)
        self.assertEqual(pack[-1], rev(1))

    def test_a_copy_that_does_not_match_keeps_the_guest_record(self) -> None:
        guest = self.guest(corrupt=True)
        with self.assertRaises(RuntimeError):
            relay.archive(guest, self.dest, limit=5)
        self.assertIn(rev(1), guest.records)
        self.assertEqual(guest.outbox, {})
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_the_limit_bounds_one_invocation(self) -> None:
        guest = self.guest()
        result = relay.archive(guest, self.dest, limit=1)
        self.assertEqual([m["record"] for m in result["archived"]], [rev(1)])
        self.assertIn(rev(2), guest.records)

    def test_nightly_records_get_a_flat_file_name(self) -> None:
        night = "nightly/2026-10-17T01-30Z-c6d02f0"
        guest = FakeGuest({night: NOW - 10 * HOUR})
        relay.archive_record(guest, night, self.dest)  # selection is tested above
        self.assertTrue((self.dest / "nightly-2026-10-17T01-30Z-c6d02f0.tar").is_file())
        self.assertNotIn(night, guest.records)

    def test_only_ci_record_names_are_ever_removed(self) -> None:
        for bad in (
            "../etc",
            "toolchain-provenance-b838d34bba223857",
            rev(1) + "/x",
            "",
        ):
            with self.assertRaises(ValueError):
                relay.archive_record(FakeGuest({}), bad, self.dest)

    def test_a_missing_archive_directory_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            relay.archive(self.guest(), self.dest / "absent", limit=3)

    def test_archives_older_than_ninety_days_are_pruned(self) -> None:
        fresh, stale = self.dest / f"{rev(1)}.tar", self.dest / f"{rev(2)}.tar"
        other = self.dest / "notes.txt"
        for path in (fresh, stale, other):
            path.write_bytes(b"x")
        old = time.time() - 91 * 86400
        os.utime(stale, (old, old))
        os.utime(other, (old, old))
        self.assertEqual(relay.prune_archive(self.dest), [stale.name])
        self.assertTrue(fresh.exists() and other.exists())


class CommandTests(unittest.TestCase):
    def test_dominus_being_off_is_not_a_failure(self) -> None:
        class Off:
            def run(self, argv: list[str], timeout: float = 120) -> str:
                raise relay.HostUnreachable("dominus-nobara unreachable")

        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                command="archive", dest=Path(tmp), limit=3, state=Path(tmp) / "state"
            )
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(relay._run(args, Off()), 0)
        self.assertIn("Dominus host unreachable", out.getvalue())

    def test_relay_commands_share_one_guest_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "state" / "relay.lock"
            with relay.guest_lock(lock):
                self.assertTrue(lock.exists())
                import fcntl

                with lock.open("w") as other, self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == "__main__":
    unittest.main()
