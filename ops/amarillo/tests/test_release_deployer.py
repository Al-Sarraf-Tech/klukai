from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import klukai_compose  # noqa: E402
import klukai_secrets  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "klukai_release", SCRIPTS / "klukai-release.py"
)
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)

# A stand-in for systemd-creds: "encryption" binds the credential name and the
# chosen key, so wrong names fail exactly like the real tool.
FAKE_CREDS = """#!/usr/bin/env python3
import base64, json, sys
args = [a for a in sys.argv[1:] if a != "--user"]
cmd, opts = args[0], [a for a in args[1:] if a.startswith("--")]
src, dst = [a for a in args[1:] if not a.startswith("--")]
name = next(o.split("=", 1)[1] for o in opts if o.startswith("--name="))
data = sys.stdin.buffer.read() if src == "-" else open(src, "rb").read()
if cmd == "encrypt":
    out = json.dumps({"name": name, "data": base64.b64encode(data).decode()}).encode()
else:
    try:
        doc = json.loads(data)
    except ValueError:
        sys.exit("not a credential")
    if doc["name"] != name:
        sys.exit("name mismatch")
    out = base64.b64decode(doc["data"])
if dst == "-":
    sys.stdout.buffer.write(out)
else:
    open(dst, "wb").write(out)
"""


class SecretStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        fake = root / "systemd-creds"
        fake.write_text(FAKE_CREDS)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.store = root / "store"
        self.runtime = root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.patches = [
            mock.patch.object(klukai_secrets, "SYSTEMD_CREDS", str(fake)),
            mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime)}),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def test_encrypt_keeps_previous_and_a_matching_recovery_copy(self) -> None:
        klukai_secrets.encrypt("compose-env", b"A=1\n", self.store)
        klukai_secrets.encrypt("compose-env", b"A=2\n", self.store)
        klukai_secrets.encrypt("core-override", b"{}", self.store)
        self.assertEqual(klukai_secrets.decrypt("compose-env", self.store), b"A=2\n")
        self.assertTrue((self.store / "compose-env.cred.prev").is_file())
        mode = (self.store / "recovery/compose-env.cred").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertTrue(klukai_secrets.check(self.store)["plaintext_absent"])

    def test_rollback_and_recovery_restore_the_previous_value(self) -> None:
        klukai_secrets.encrypt("compose-env", b"A=1\n", self.store)
        klukai_secrets.encrypt("compose-env", b"A=2\n", self.store)
        klukai_secrets.rollback("compose-env", self.store)
        self.assertEqual(klukai_secrets.decrypt("compose-env", self.store), b"A=1\n")
        (self.store / "compose-env.cred").unlink()
        klukai_secrets.recover("compose-env", self.store)
        self.assertEqual(klukai_secrets.decrypt("compose-env", self.store), b"A=1\n")

    def test_missing_corrupt_or_exposed_credentials_fail_closed(self) -> None:
        with self.assertRaises(klukai_secrets.SecretError):
            klukai_secrets.decrypt("compose-env", self.store)  # no store at all
        klukai_secrets.encrypt("compose-env", b"A=1\n", self.store)
        cred = self.store / "compose-env.cred"
        cred.chmod(0o644)
        with self.assertRaises(klukai_secrets.SecretError):
            klukai_secrets.decrypt("compose-env", self.store)
        cred.chmod(0o600)
        cred.write_bytes(b"garbage")
        with self.assertRaises(klukai_secrets.SecretError):
            klukai_secrets.decrypt("compose-env", self.store)
        with self.assertRaises(klukai_secrets.SecretError):
            klukai_secrets.encrypt("compose-env", b"", self.store)

    def test_plaintext_lives_only_in_runtime_dir_for_one_command(self) -> None:
        klukai_secrets.encrypt("compose-env", b"A=1\n", self.store)
        klukai_secrets.encrypt("core-override", b"{}", self.store)
        with klukai_secrets.materialized(klukai_secrets.NAMES, self.store) as paths:
            directory = paths["compose-env"].parent
            self.assertTrue(directory.is_relative_to(self.runtime))
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(paths["compose-env"].read_bytes(), b"A=1\n")
            argv = klukai_compose.argv(paths, ["ps"], pin=Path("/nonexistent"))
            self.assertEqual(
                argv[:4], ["docker", "compose", "--env-file", str(paths["compose-env"])]
            )
        self.assertFalse(directory.exists())

    def test_check_refuses_leftover_plaintext(self) -> None:
        klukai_secrets.encrypt("compose-env", b"A=1\n", self.store)
        klukai_secrets.encrypt("core-override", b"{}", self.store)
        leftover = Path(self.tmp.name) / ".env"
        leftover.write_text("A=1\n")
        with self.assertRaises(klukai_secrets.SecretError):
            klukai_secrets.check(self.store, (leftover,))


class FakeTarget(release.Target):
    """Containers in memory: an image is healthy unless it is marked bad."""

    def __init__(self, pin: Path, images: dict[str, str], bad: set[str]) -> None:
        super().__init__(
            compose=self._compose,
            container="core",
            pin=pin,
            ready=lambda: self.running not in self.bad,
        )
        self.images, self.bad = images, bad
        self.running = images[self.read_pin()] if pin.exists() else ""
        self.recreated = 0

    def _compose(self, args: list[str]) -> None:
        self.recreated += 1
        self.running = self.images[self.read_pin()]

    def inspect(self, template: str) -> str:
        if template == "{{.Image}}":
            return self.running
        if template == "{{.State.Health.Status}}":
            return "unhealthy" if self.running in self.bad else "healthy"
        return "0"


class RolloutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.pin = Path(self.tmp.name) / "pin.json"
        self.tags: dict[str, str] = {}

        def fake_run(argv: list[str], **kwargs: object) -> str:
            if argv[:2] == ["docker", "tag"]:
                self.tags[argv[3]] = argv[2]
            return ""

        self.patch = mock.patch.object(release, "run", side_effect=fake_run)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.tmp.cleanup()

    def target(self, bad: set[str]) -> FakeTarget:
        images = {"klukai-core:old": "sha256:old", "klukai-core:new": "sha256:new"}

        class Resolving(dict):
            def __missing__(inner, key: str) -> str:
                return self.tags[key]

        self.pin.write_text(
            json.dumps({"services": {"companion-core": {"image": "klukai-core:old"}}})
        )
        return FakeTarget(self.pin, Resolving(images), bad)

    def fast(self) -> dict:
        ticks = iter(range(10_000))
        return {
            "ready_timeout": 3,
            "hold": 3,
            "interval": 1,
            "clock": lambda: next(ticks),
            "sleep": lambda _: None,
        }

    def test_healthy_candidate_is_kept(self) -> None:
        target = self.target(bad=set())
        result = release.rollout(target, "klukai-core:new", "sha256:new", **self.fast())
        self.assertEqual(result["status"], "deployed")
        self.assertEqual(target.running, "sha256:new")
        self.assertEqual(target.read_pin(), "klukai-core:new")
        self.assertEqual(self.tags[result["rollback_tag"]], "sha256:old")

    def test_unhealthy_candidate_is_rolled_back_to_the_exact_previous_image(
        self,
    ) -> None:
        target = self.target(bad={"sha256:new"})
        result = release.rollout(target, "klukai-core:new", "sha256:new", **self.fast())
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(target.running, "sha256:old")
        self.assertEqual(target.read_pin(), result["rollback_tag"])
        self.assertEqual(target.recreated, 2)

    def test_an_error_burst_during_the_hold_triggers_rollback(self) -> None:
        target = self.target(bad=set())
        target.errors = lambda since: 0.2 if target.running == "sha256:new" else 0.0
        result = release.rollout(target, "klukai-core:new", "sha256:new", **self.fast())
        self.assertEqual(result["status"], "rolled_back")
        self.assertIn("5xx", result["detail"])

    def test_failed_rollback_is_reported_not_hidden(self) -> None:
        target = self.target(bad={"sha256:new", "sha256:old"})
        result = release.rollout(target, "klukai-core:new", "sha256:new", **self.fast())
        self.assertEqual(result["status"], "rollback_failed")

    def test_nothing_running_means_no_rollout(self) -> None:
        target = self.target(bad=set())
        target.running = ""
        with self.assertRaises(release.ReleaseError):
            release.rollout(target, "klukai-core:new", "sha256:new", **self.fast())


class ArtifactGateTests(unittest.TestCase):
    def test_unsigned_or_wrongly_signed_provenance_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = root / "provenance.json"
            doc.write_text("{}")
            with self.assertRaises(release.ReleaseError):
                release.verify_signature(doc, root / "signers")
            key = root / "other"
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                check=True,
            )
            subprocess.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-Y",
                    "sign",
                    "-f",
                    str(key),
                    "-n",
                    "klukai-release",
                    str(doc),
                ],
                check=True,
            )
            # The committed signer list does not contain this key.
            with self.assertRaises(release.ReleaseError):
                release.verify_signature(doc)

    def test_committed_signer_is_scoped_to_the_release_namespace(self) -> None:
        line = release.SIGNERS.read_text().strip()
        self.assertTrue(
            line.startswith(
                'klukai-release@amarillo namespaces="klukai-release" ssh-ed25519 '
            )
        )


if __name__ == "__main__":
    unittest.main()
