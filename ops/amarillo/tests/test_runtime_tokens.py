"""SEC-3: the metrics and alert-bridge tokens live only in the encrypted store and
in /run, written at boot by a system unit ordered before Docker."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

OPS = Path(__file__).resolve().parents[1]
REPO = OPS.parents[1]
sys.path.insert(0, str(OPS / "scripts"))
sys.path.insert(0, str(OPS / "tests"))
import klukai_compose  # noqa: E402
import klukai_runtime_tokens as tokens  # noqa: E402
import klukai_secrets  # noqa: E402
from test_release_deployer import FAKE_CREDS  # noqa: E402

UNIT = OPS / "systemd/klukai-runtime-secrets.service"
BRIDGE_UNIT = OPS / "systemd/klukai-alert-bridge.service"
METRICS = "m" * 64
BRIDGE = "b-" + "c" * 62


class RuntimeTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        fake = root / "systemd-creds"
        fake.write_text(FAKE_CREDS)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.store = root / "store"
        self.runtime = root / "run/klukai-secrets"
        # What systemd's RuntimeDirectory= creates: 0700 directories, this user.
        for directory in ("", "metrics", "alert-bridge"):
            (self.runtime / directory).mkdir(mode=0o700, parents=True)
        patch = mock.patch.object(klukai_secrets, "SYSTEMD_CREDS", str(fake))
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)
        klukai_secrets.encrypt("metrics-token", METRICS.encode() + b"\n", self.store)
        klukai_secrets.encrypt("alert-bridge-token", BRIDGE.encode(), self.store)

    def path(self, name: str) -> Path:
        return self.runtime / tokens.LAYOUT[name] / name

    def test_install_writes_each_token_0400_into_its_own_directory(self) -> None:
        tokens.install(self.runtime, self.store)
        self.assertEqual(self.path("metrics-token").read_text(), METRICS)
        self.assertEqual(self.path("alert-bridge-token").read_text(), BRIDGE)
        for name in tokens.LAYOUT:
            folder = self.path(name).parent
            self.assertEqual(stat.S_IMODE(self.path(name).stat().st_mode), 0o400)
            # Each consumer's directory holds only its own token.
            self.assertEqual([p.name for p in folder.iterdir()], [name])
        self.assertTrue(tokens.check(self.runtime, self.store)["match_store"])

    def test_rotation_replaces_the_file_inside_the_same_directory(self) -> None:
        tokens.install(self.runtime, self.store)
        folder = self.path("metrics-token").parent
        inode = folder.stat().st_ino
        klukai_secrets.encrypt("metrics-token", b"r" * 64, self.store)
        tokens.install(self.runtime, self.store)
        self.assertEqual(folder.stat().st_ino, inode)  # bind mounts keep seeing it
        self.assertEqual(self.path("metrics-token").read_text(), "r" * 64)

    def test_remove_deletes_only_the_files_and_keeps_the_directories(self) -> None:
        tokens.install(self.runtime, self.store)
        (self.path("metrics-token").parent / ".metrics-token-stale").write_text("x")
        tokens.remove(self.runtime)
        for name in tokens.LAYOUT:
            self.assertTrue(self.path(name).parent.is_dir())
            self.assertEqual(list(self.path(name).parent.iterdir()), [])
        with self.assertRaises(OSError):
            tokens.check(self.runtime, self.store)
        tokens.remove(self.runtime)  # idempotent (ExecStop after a failed start)

    def test_a_bad_credential_writes_nothing(self) -> None:
        for bad in (b"short", b"has space " + b"x" * 40, b"\xff" * 40):
            klukai_secrets.encrypt("alert-bridge-token", bad, self.store)
            with self.assertRaises(klukai_secrets.SecretError):
                tokens.install(self.runtime, self.store)
            self.assertFalse(self.path("metrics-token").exists())
        (self.store / "alert-bridge-token.cred").unlink()
        with self.assertRaises(klukai_secrets.SecretError):
            tokens.install(self.runtime, self.store)
        self.assertFalse(self.path("metrics-token").exists())

    def test_install_refuses_a_shared_directory(self) -> None:
        self.path("metrics-token").parent.chmod(0o750)
        with self.assertRaises(tokens.secret_store.CredentialError):
            tokens.install(self.runtime, self.store)

    def test_check_detects_a_stale_or_exposed_copy(self) -> None:
        tokens.install(self.runtime, self.store)
        klukai_secrets.encrypt("metrics-token", b"n" * 64, self.store)
        with self.assertRaises(klukai_secrets.SecretError):
            tokens.check(self.runtime, self.store)
        tokens.install(self.runtime, self.store)
        self.path("alert-bridge-token").chmod(0o440)
        with self.assertRaises(klukai_secrets.SecretError):
            tokens.check(self.runtime, self.store)

    def test_cli_never_prints_a_token(self) -> None:
        env = dict(os.environ, PATH=f"{Path(self.tmp.name)}:{os.environ['PATH']}")
        script = OPS / "scripts/klukai_runtime_tokens.py"
        for command in ("install", "check", "remove"):
            result = subprocess.run(
                [sys.executable, "-I", str(script), command]
                + ["--runtime", str(self.runtime), "--store", str(self.store)],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(METRICS, result.stdout + result.stderr)
            self.assertNotIn(BRIDGE, result.stdout + result.stderr)


class BootUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.unit = UNIT.read_text()
        self.lines = [
            line.strip() for line in self.unit.splitlines() if not line.startswith("#")
        ]

    def values(self, key: str) -> list[str]:
        return [
            line.split("=", 1)[1] for line in self.lines if line.startswith(key + "=")
        ]

    def test_runs_before_docker_without_making_docker_depend_on_it(self) -> None:
        self.assertEqual(self.values("Before"), ["docker.service"])
        for key in ("Requires", "BindsTo", "RequiredBy", "PartOf"):
            self.assertEqual(self.values(key), [], key)
        self.assertEqual(self.values("WantedBy"), ["multi-user.target"])
        self.assertEqual(self.values("Type"), ["oneshot"])
        self.assertEqual(self.values("RemainAfterExit"), ["yes"])

    def test_runs_as_the_owner_never_root_and_keeps_directories(self) -> None:
        self.assertEqual(self.values("User"), ["jalsarraf"])
        self.assertEqual(self.values("NoNewPrivileges"), ["yes"])
        self.assertEqual(self.values("CapabilityBoundingSet"), [""])
        self.assertEqual(self.values("RuntimeDirectoryMode"), ["0700"])
        self.assertEqual(self.values("RuntimeDirectoryPreserve"), ["yes"])
        directories = self.values("RuntimeDirectory")[0].split()
        expected = {f"klukai-secrets/{d}" for d in tokens.LAYOUT.values()}
        self.assertEqual(set(directories), {"klukai-secrets"} | expected)
        self.assertEqual(Path("/run") / "klukai-secrets", tokens.RUNTIME)

    def test_exec_lines_run_this_script(self) -> None:
        for key, command in (("ExecStart", "install"), ("ExecStop", "remove")):
            (line,) = self.values(key)
            argv = line.split()
            self.assertEqual(argv[:2], ["/usr/bin/python3", "-I"])
            self.assertTrue(
                argv[2].endswith("ops/amarillo/scripts/klukai_runtime_tokens.py")
            )
            self.assertEqual(argv[3:], [command])


class ConsumerWiringTests(unittest.TestCase):
    def test_no_consumer_reads_the_old_plaintext_directory(self) -> None:
        for path in ("docker-compose.yml", "docker-compose.obs.yml"):
            self.assertNotIn(".runtime-secrets", (REPO / path).read_text(), path)
        self.assertNotIn(".runtime-secrets", BRIDGE_UNIT.read_text())
        self.assertIn(
            "--token-file /run/klukai-secrets/alert-bridge/alert-bridge-token",
            BRIDGE_UNIT.read_text(),
        )

    def test_core_and_obs_mount_only_their_token_directories(self) -> None:
        core = (REPO / "docker-compose.yml").read_text()
        self.assertIn(
            "${KLUKAI_RUNTIME_SECRETS_DIR:-/run/klukai-secrets/metrics}:/run/klukai-secrets:ro",
            core,
        )
        obs = (REPO / "docker-compose.obs.yml").read_text()
        self.assertIn(
            "${KLUKAI_RUNTIME_SECRETS_DIR:-/run/klukai-secrets/metrics}:/etc/prometheus/secrets:ro",
            obs,
        )
        self.assertIn(
            "${KLUKAI_ALERT_BRIDGE_SECRETS_DIR:-/run/klukai-secrets/alert-bridge}"
            ":/etc/alertmanager/secrets:ro",
            obs,
        )

    def guard(self, binary: str) -> str:
        obs = (REPO / "docker-compose.obs.yml").read_text()
        match = re.search(r"- '(\[ -s .*exec /bin/%s \"\$\$@\")'" % binary, obs)
        assert match, binary
        return match.group(1).replace("$$", "$")

    def test_obs_containers_refuse_a_missing_or_empty_token(self) -> None:
        for binary, secret in (
            ("prometheus", "/etc/prometheus/secrets/metrics-token"),
            ("alertmanager", "/etc/alertmanager/secrets/alert-bridge-token"),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                token = Path(tmp) / "token"
                script = self.guard(binary)
                self.assertIn(secret, script)
                script = script.replace(secret, str(token))
                script = script.replace(f"/bin/{binary}", "/bin/echo")

                def start() -> subprocess.CompletedProcess[str]:
                    return subprocess.run(
                        ["/bin/sh", "-c", script, binary, "--started"],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )

                missing = start()
                self.assertEqual(missing.returncode, 78)
                self.assertIn("token missing", missing.stderr)
                token.write_text("")
                self.assertEqual(start().returncode, 78)
                token.write_text("x" * 64)
                started = start()
                self.assertEqual(started.returncode, 0)
                self.assertEqual(started.stdout.strip(), "--started")

    def test_obs_compose_path_uses_no_store_secrets_and_no_dotenv(self) -> None:
        argv = klukai_compose.obs_argv(["up", "-d", "--no-deps", "prometheus"])
        self.assertEqual(
            argv[:6], ["docker", "compose", "-p", "klukai", "--env-file", os.devnull]
        )
        self.assertEqual(argv[6:8], ["-f", str(REPO / "docker-compose.obs.yml")])
        self.assertEqual(argv[8:], ["up", "-d", "--no-deps", "prometheus"])


if __name__ == "__main__":
    unittest.main()
