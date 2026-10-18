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
key = next((o.split("=", 1)[1] for o in opts if o.startswith("--with-key=")), None)
data = sys.stdin.buffer.read() if src == "-" else open(src, "rb").read()
if cmd == "encrypt":
    out = json.dumps(
        {"name": name, "key": key, "data": base64.b64encode(data).decode()}
    ).encode()
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
        with self.assertRaises(klukai_secrets.SecretError):
            klukai_secrets.check(self.store)  # the runtime tokens are not stored yet
        for name in klukai_secrets.TOKEN_NAMES:
            klukai_secrets.encrypt(name, b"t" * 64, self.store)
        self.assertTrue(klukai_secrets.check(self.store)["plaintext_absent"])

    def test_primary_copies_require_tpm2_and_recovery_copies_the_host_key(
        self,
    ) -> None:
        # Independent review finding 4: "auto" may silently fall back to the
        # host key alone, so the primary policy is requested explicitly.
        klukai_secrets.encrypt("compose-env", b"A=1\n", self.store)
        primary = json.loads((self.store / "compose-env.cred").read_text())
        rescue = json.loads((self.store / "recovery/compose-env.cred").read_text())
        self.assertEqual(primary["key"], "host+tpm2")
        self.assertEqual(rescue["key"], "host")

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
        with klukai_secrets.materialized(klukai_secrets.COMPOSE_NAMES, self.store) as paths:
            directory = paths["compose-env"].parent
            self.assertTrue(directory.is_relative_to(self.runtime))
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(paths["compose-env"].read_bytes(), b"A=1\n")
            # Compose never sees the runtime tokens; those live only in /run.
            self.assertEqual(sorted(paths), ["compose-env", "core-override"])
            self.assertEqual(sorted(p.name for p in directory.iterdir()), sorted(paths))
            argv = klukai_compose.argv(paths, ["ps"], pin=Path("/nonexistent"))
            self.assertEqual(
                argv[:4], ["docker", "compose", "--env-file", str(paths["compose-env"])]
            )
        self.assertFalse(directory.exists())

    def test_stale_plaintext_from_a_killed_command_is_swept(self) -> None:
        stale = self.runtime / "klukai-secrets-old"
        stale.mkdir(mode=0o700)
        (stale / "compose-env").write_text("A=1\n")
        os.utime(stale, (0, 0))
        fresh = self.runtime / "klukai-secrets-live"
        fresh.mkdir(mode=0o700)
        self.assertEqual(klukai_secrets.sweep_stale(self.runtime), 1)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_check_refuses_leftover_plaintext(self) -> None:
        klukai_secrets.encrypt("compose-env", b"A=1\n", self.store)
        klukai_secrets.encrypt("core-override", b"{}", self.store)
        for name in klukai_secrets.TOKEN_NAMES:
            klukai_secrets.encrypt(name, b"t" * 64, self.store)
        self.assertTrue(klukai_secrets.check(self.store)["plaintext_absent"])
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


class ReviewFindingTests(RolloutTests):
    """Regressions for the 2026-10-17 fresh-context review."""

    def test_compose_failure_after_the_pin_switch_still_rolls_back(self) -> None:
        target = self.target(bad=set())
        real = target._compose
        calls = []

        def flaky(args: list[str]) -> None:
            calls.append(target.read_pin())
            if len(calls) == 1:
                raise release.ReleaseError("docker compose up failed: port in use")
            real(args)

        target.compose = flaky
        result = release.rollout(target, "klukai-core:new", "sha256:new", **self.fast())
        self.assertEqual(result["status"], "rolled_back")
        self.assertIn("rollout interrupted", result["detail"])
        self.assertEqual(target.read_pin(), result["rollback_tag"])
        self.assertEqual(target.running, "sha256:old")

    def test_interrupt_during_the_hold_rolls_back(self) -> None:
        target = self.target(bad=set())
        ticks = iter(range(10_000))

        def sleep(_: float) -> None:
            if target.running == "sha256:new":
                raise KeyboardInterrupt

        result = release.rollout(
            target,
            "klukai-core:new",
            "sha256:new",
            ready_timeout=3,
            hold=3,
            interval=1,
            clock=lambda: next(ticks),
            sleep=sleep,
        )
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(target.running, "sha256:old")

    def test_a_hold_that_ends_on_failed_checks_is_not_deployed(self) -> None:
        target = self.target(bad=set())
        answers = iter([True])  # ready once, then failing (fewer than 3 checks fit)

        def ready() -> bool:
            if target.running != "sha256:new":
                return True
            return next(answers, False)

        target.ready = ready
        result = release.rollout(target, "klukai-core:new", "sha256:new", **self.fast())
        self.assertEqual(result["status"], "rolled_back")
        self.assertIn("the hold ended with", result["detail"])

    def test_error_query_uses_the_exported_status_class_label(self) -> None:
        query = release.error_query(180)
        self.assertIn('status_class="5xx"', query)
        self.assertNotIn("status=~", query)
        self.assertIn("or vector(0)", query)

    def test_error_ratio_tells_no_traffic_from_unreadable_metrics(self) -> None:
        # Independent review finding 3: a failed query used to read as "no
        # traffic" and pass the 5xx gate.
        def response(result: object, status: str = "success") -> mock.MagicMock:
            body = json.dumps({"status": status, "data": {"result": result}}).encode()
            handle = mock.MagicMock()
            handle.__enter__.return_value.read.return_value = body
            return handle

        up = response([{"value": [0, "1"]}])
        down = response([{"value": [0, "0"]}])
        cases = (
            ([up, response([{"value": [0, "0.25"]}])], 0.25),
            ([up, response([{"value": [0, "NaN"]}])], None),
            ([up, response([])], None),
        )
        for answers, expected in cases:
            with mock.patch.object(
                release.urllib.request, "urlopen", side_effect=answers
            ):
                self.assertEqual(release.error_ratio(0), expected)
        failing = (
            [release.urllib.error.URLError("connection refused")],
            [down],
            [response([])],
            [response([], status="error")],
            [up, release.urllib.error.URLError("timed out")],
            [up, response({"not": "a list"})],
        )
        for answers in failing:
            with (
                mock.patch.object(
                    release.urllib.request, "urlopen", side_effect=answers
                ),
                self.assertRaises(release.MetricsUnavailable),
            ):
                release.error_ratio(0)

    def test_prune_keeps_the_newest_dumps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backups = Path(tmp)
            for i in range(12):
                (backups / f"aichat-20261017T{i:06d}Z.dump").write_text("x")
            self.assertEqual(release.prune(backups)["dumps_removed"], 2)
            self.assertEqual(len(list(backups.glob("*.dump"))), 10)
            self.assertTrue((backups / "aichat-20261017T000011Z.dump").exists())


class RollbackSafetyTests(unittest.TestCase):
    def test_a_release_that_changes_migrations_needs_operator_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", "-C", str(repo), *args],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()

            git("init", "-q")
            git("config", "user.email", "t@t")
            git("config", "user.name", "t")
            (repo / "docker-compose.yml").write_text("services: {}\n")
            git("add", ".")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            (repo / "README").write_text("docs\n")
            git("add", ".")
            git("commit", "-qm", "docs")
            docs_only = git("rev-parse", "HEAD")
            (repo / "docker/core/migrations").mkdir(parents=True)
            (repo / "docker/core/migrations/0099.sql").write_text("select 1;\n")
            git("add", ".")
            git("commit", "-qm", "migration")
            migration = git("rev-parse", "HEAD")
            real_run = subprocess.run

            def fake(argv, **kwargs):
                if argv[:2] == ["docker", "inspect"]:
                    return subprocess.CompletedProcess(argv, 0, base + "\n", "")
                return real_run(argv, **kwargs)

            with mock.patch.object(release.subprocess, "run", side_effect=fake):
                self.assertTrue(release.rollback_safety(docs_only, repo)["safe"])
                unsafe = release.rollback_safety(migration, repo)
            self.assertFalse(unsafe["safe"])
            self.assertEqual(unsafe["changed"], ["docker/core/migrations/0099.sql"])


class IndependentReviewTests(RolloutTests):
    """Regressions for the 2026-10-18 independent Codex review (PROC-1)."""

    def test_an_interrupt_right_after_the_pin_switch_rolls_back(self) -> None:
        # Finding 2: the pin switch ran before the protected region, so an
        # interrupt there left the unchecked candidate pinned.
        target = self.target(bad=set())
        real = target.write_pin

        def write_then_interrupt(image: str | None) -> None:
            real(image)
            if image == "klukai-core:new":
                raise KeyboardInterrupt

        target.write_pin = write_then_interrupt  # type: ignore[method-assign]
        result = release.rollout(target, "klukai-core:new", "sha256:new", **self.fast())
        self.assertEqual(result["status"], "rolled_back")
        self.assertIn("KeyboardInterrupt", result["detail"])
        self.assertEqual(target.read_pin(), result["rollback_tag"])
        self.assertEqual(target.running, "sha256:old")

    def test_unreadable_metrics_fail_the_hold(self) -> None:
        # Finding 3: the 5xx gate must be evaluated, not assumed.
        target = self.target(bad=set())

        def errors(since: float) -> float | None:
            if target.running == "sha256:new":
                raise release.MetricsUnavailable("Prometheus query failed")
            return 0.0

        target.errors = errors
        result = release.rollout(target, "klukai-core:new", "sha256:new", **self.fast())
        self.assertEqual(result["status"], "rolled_back")
        self.assertIn("could not be evaluated", result["detail"])
        self.assertEqual(target.running, "sha256:old")

    def test_a_transient_metrics_failure_is_retried(self) -> None:
        target = self.target(bad=set())
        answers = iter([release.MetricsUnavailable("blip"), 0.0])

        def errors(since: float) -> float | None:
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        target.errors = errors
        result = release.rollout(target, "klukai-core:new", "sha256:new", **self.fast())
        self.assertEqual(result["status"], "deployed")
        self.assertIn("5xx ratio 0.00% during the hold", result["checks"])

    def test_verified_image_is_bound_by_config_and_deployed_by_id(self) -> None:
        # Finding 1: layers and the revision label alone let a config-only
        # replacement (Entrypoint/Cmd/Env) through.
        revision = "c" * 40
        image_id = "sha256:" + "d" * 64
        manifest = {"layers": ["sha256:" + "e" * 64], "config_sha256": "f" * 64}
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / revision
            directory.mkdir()
            (directory / "manifest.json").write_text(json.dumps(manifest))
            (directory / "provenance.json").write_text(
                json.dumps(
                    {
                        "revision": revision,
                        "manifest_sha256": release.release_artifact.sha256(
                            directory / "manifest.json"
                        ),
                    }
                )
            )
            calls: list[list[str]] = []

            def fake_run(argv: list[str], **kwargs: object) -> str:
                calls.append(argv)
                return image_id + "\n" if argv[:3] == ["docker", "image", "inspect"] else ""

            def check(found: dict) -> dict:
                with (
                    mock.patch.object(release, "verify_signature"),
                    mock.patch.object(release.release_artifact, "check_manifest"),
                    mock.patch.object(
                        release.release_artifact, "verify_materials", return_value={}
                    ),
                    mock.patch.object(
                        release.release_artifact, "identity", return_value=found
                    ) as identity,
                    mock.patch.object(release, "run", side_effect=fake_run),
                ):
                    result = release.verify_artifact(revision, Path(tmp))
                identity.assert_called_once_with(image_id)  # by immutable ID
                return result

            good = {**manifest, "revision": revision}
            result = check(good)
            self.assertEqual(result["image_id"], image_id)
            self.assertEqual(
                result["tag"], f"klukai-core:verified-{revision[:7]}-{'d' * 12}"
            )
            self.assertIn(["docker", "tag", image_id, result["tag"]], calls)
            for name, value in (("config_sha256", "0" * 64), ("revision", "b" * 40)):
                with self.assertRaises(release.ReleaseError):
                    check({**good, name: value})


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
