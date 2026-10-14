"""Credential pickup integration tests; every credential here is a fake fixture."""

import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("pickup_gateway_token.py")
FIRST = b"mk_aaaaaaaaaaaa_" + b"A" * 43
SECOND = b"mk_bbbbbbbbbbbb_" + b"B" * 43
OTHER = b"mk_cccccccccccc_" + b"C" * 43


class TokenPickupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "drops"
        self.source.mkdir()
        self.destination = self.base / "private" / "gateway.token"

    def drop(self, name, token=FIRST, timestamp=100):
        path = self.source / name
        path.write_bytes(token + b"\n")
        os.utime(path, ns=(timestamp * 10**9, timestamp * 10**9))
        return path

    def pickup(self):
        return subprocess.run(
            [
                sys.executable, str(SCRIPT),
                "--source-dir", str(self.source),
                "--destination", str(self.destination),
                "--owner-uid", str(os.getuid()),
                "--owner-gid", str(os.getgid()),
            ],
            capture_output=True,
            timeout=5,
        )

    def assert_safe_failure(self, result):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, b"")
        for secret in (FIRST, SECOND, OTHER):
            self.assertNotIn(secret, result.stderr)

    def test_only_exact_client_filename_is_eligible(self):
        self.drop("klukai-web-tools_20261014T120000Z.txt")
        for name in (
            "other-client_20261014T130000Z.txt",
            "klukai-web-tools-extra_20261014T130000Z.txt",
            "klukai-web-tools_20261014T130000Z.txt.bak",
            "klukai-web-tools_20261314T130000Z.txt",
        ):
            self.drop(name, OTHER, timestamp=300)
        # An unrelated unreadable/nonregular entry must not be opened or inspected.
        os.mkfifo(self.source / "other-client_20261014T140000Z.txt")
        result = self.pickup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.destination.read_bytes(), FIRST + b"\n")
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")

    def test_newest_rotation_is_published_atomically(self):
        self.drop("klukai-web-tools_20261014T120000Z.txt")
        self.assertEqual(self.pickup().returncode, 0)
        previous = self.destination.open("rb")
        self.addCleanup(previous.close)
        self.drop("klukai-web-tools_20261021T120000Z.txt", SECOND, timestamp=200)
        result = self.pickup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.destination.read_bytes(), SECOND + b"\n")
        self.assertEqual(previous.read(), FIRST + b"\n")
        self.assertEqual(self.destination.stat().st_mtime_ns, 200 * 10**9)
        self.assertEqual([p.name for p in self.destination.parent.iterdir()], ["gateway.token"])

    def test_old_or_equal_mtime_does_not_replace_destination(self):
        self.drop("klukai-web-tools_20261014T120000Z.txt", FIRST, timestamp=200)
        self.assertEqual(self.pickup().returncode, 0)
        inode = self.destination.stat().st_ino
        for timestamp in (100, 200):
            with self.subTest(timestamp=timestamp):
                self.drop("klukai-web-tools_20261021T120000Z.txt", SECOND, timestamp=timestamp)
                result = self.pickup()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.destination.stat().st_ino, inode)
                self.assertEqual(self.destination.read_bytes(), FIRST + b"\n")

    def test_empty_source_leaves_existing_destination_untouched(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(FIRST + b"\n")
        before = self.destination.stat()
        result = self.pickup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.destination.read_bytes(), FIRST + b"\n")
        self.assertEqual(self.destination.stat().st_mtime_ns, before.st_mtime_ns)

    def test_no_own_drop_does_not_create_destination(self):
        self.drop("other-client_20261014T130000Z.txt", OTHER)
        result = self.pickup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.destination.exists())

    def test_permissions_and_ownership_are_private(self):
        self.destination.parent.mkdir(mode=0o755)
        self.drop("klukai-web-tools_20261014T120000Z.txt")
        result = self.pickup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(self.destination.parent.stat().st_mode), 0o700)
        token_stat = self.destination.stat()
        self.assertEqual(stat.S_IMODE(token_stat.st_mode), 0o600)
        self.assertEqual((token_stat.st_uid, token_stat.st_gid), (os.getuid(), os.getgid()))
        parent_stat = self.destination.parent.stat()
        self.assertEqual((parent_stat.st_uid, parent_stat.st_gid), (os.getuid(), os.getgid()))

    def test_invalid_newest_token_fails_without_replacing_valid_destination(self):
        self.drop("klukai-web-tools_20261014T120000Z.txt")
        self.assertEqual(self.pickup().returncode, 0)
        for invalid in (b"invalid", b"mk_" + b"A" * 12 + b"_" + b"B" * 43,
                        SECOND + b"\nextra", SECOND + b" ", b"x" * 100000):
            with self.subTest(length=len(invalid)):
                self.drop("klukai-web-tools_20261021T120000Z.txt", invalid, timestamp=200)
                result = self.pickup()
                self.assert_safe_failure(result)
                self.assertNotIn(invalid[:50], result.stderr)
                self.assertEqual(self.destination.read_bytes(), FIRST + b"\n")

    def test_source_symlink_is_rejected(self):
        target = self.base / "unrelated.token"
        target.write_bytes(OTHER + b"\n")
        (self.source / "klukai-web-tools_20261014T120000Z.txt").symlink_to(target)
        self.assert_safe_failure(self.pickup())
        self.assertFalse(self.destination.exists())

    def test_source_fifo_is_rejected_without_blocking(self):
        os.mkfifo(self.source / "klukai-web-tools_20261014T120000Z.txt")
        self.assert_safe_failure(self.pickup())
        self.assertFalse(self.destination.exists())

    def test_source_hard_link_is_rejected(self):
        source = self.drop("klukai-web-tools_20261014T120000Z.txt")
        os.link(source, self.base / "another-link")
        self.assert_safe_failure(self.pickup())
        self.assertFalse(self.destination.exists())

    def test_destination_hard_link_is_rejected(self):
        target = self.base / "unrelated.token"
        target.write_bytes(OTHER + b"\n")
        self.destination.parent.mkdir()
        os.link(target, self.destination)
        self.drop("klukai-web-tools_20261014T120000Z.txt", timestamp=200)
        self.assert_safe_failure(self.pickup())
        self.assertEqual(target.read_bytes(), OTHER + b"\n")
        self.assertEqual(self.destination.read_bytes(), OTHER + b"\n")

    def test_destination_symlink_is_rejected(self):
        target = self.base / "unrelated.token"
        target.write_bytes(OTHER + b"\n")
        self.destination.parent.mkdir()
        self.destination.symlink_to(target)
        self.drop("klukai-web-tools_20261014T120000Z.txt")
        self.assert_safe_failure(self.pickup())
        self.assertEqual(target.read_bytes(), OTHER + b"\n")
        self.assertTrue(self.destination.is_symlink())

    def test_destination_parent_symlink_is_rejected(self):
        target = self.base / "unrelated-directory"
        target.mkdir(mode=0o755)
        self.destination.parent.symlink_to(target, target_is_directory=True)
        self.drop("klukai-web-tools_20261014T120000Z.txt")
        self.assert_safe_failure(self.pickup())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertFalse((target / "gateway.token").exists())

    def test_source_parent_symlink_is_rejected(self):
        self.drop("klukai-web-tools_20261014T120000Z.txt")
        alias = self.base / "source-alias"
        alias.symlink_to(self.source, target_is_directory=True)
        self.source = alias
        self.assert_safe_failure(self.pickup())
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
