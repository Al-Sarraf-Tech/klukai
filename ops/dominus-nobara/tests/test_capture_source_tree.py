from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/capture-source-tree.py"
SPEC = importlib.util.spec_from_file_location("capture_source_tree", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
capture = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = capture
SPEC.loader.exec_module(capture)


class CaptureSourceTreeTests(unittest.TestCase):
    def test_manifest_is_deterministic_and_detects_content_or_mode_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "a.txt"
            second = root / "nested/b.txt"
            second.parent.mkdir()
            first.write_text("alpha\n", encoding="utf-8")
            second.write_text("beta\n", encoding="utf-8")
            first.chmod(0o644)
            second.chmod(0o755)
            allowlist = b"a.txt\0nested/b.txt\0"

            before = capture.build_manifest(root, allowlist)
            entries = [json.loads(line) for line in before.splitlines()]
            self.assertEqual([entry["path"] for entry in entries], ["a.txt", "nested/b.txt"])
            self.assertEqual([entry["mode"] for entry in entries], ["0644", "0755"])
            self.assertEqual(before, capture.build_manifest(root, allowlist))

            second.write_text("changed\n", encoding="utf-8")
            self.assertNotEqual(before, capture.build_manifest(root, allowlist))

    def test_unsorted_duplicate_and_escaping_allowlists_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a").write_text("a", encoding="utf-8")
            (root / "b").write_text("b", encoding="utf-8")
            for allowlist in (b"b\0a\0", b"a\0a\0", b"../escape\0"):
                with self.assertRaises(RuntimeError):
                    capture.build_manifest(root, allowlist)

    def test_exact_set_rejects_stale_extra_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            (root / "nested/expected.txt").write_text("expected", encoding="utf-8")
            allowlist = b"nested/expected.txt\0"

            capture.verify_exact_set(root, allowlist)
            (root / "stale.txt").write_text("stale", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, r"missing=0, extra=1"):
                capture.verify_exact_set(root, allowlist)

    def test_tracked_sensitive_path_is_rejected_not_silently_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            sensitive = root / ".env.production"
            sensitive.write_text("API_TOKEN=not-a-real-token\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(root), "add", "-f", ".env.production"],
                check=True,
            )

            allowlist = capture.git_allowlist(root)
            self.assertEqual(allowlist, b".env.production\0")
            with self.assertRaisesRegex(RuntimeError, "denied sensitive"):
                capture.build_manifest(root, allowlist)

    def test_high_confidence_secrets_in_benign_markdown_are_rejected(self) -> None:
        payloads = (
            b"notes\n-----BEGIN " + b"PRIVATE KEY-----\nredacted\n",
            b"notes\n123456789:" + (b"A" * 35) + b"\n",
        )
        for payload in payloads:
            with self.subTest(kind=payload[:20]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "notes.md").write_bytes(payload)
                with self.assertRaisesRegex(RuntimeError, "sensitive content") as error:
                    capture.build_manifest(root, b"notes.md\0")
                self.assertNotIn("notes.md", str(error.exception))
                self.assertNotIn("123456789", str(error.exception))


if __name__ == "__main__":
    unittest.main()
