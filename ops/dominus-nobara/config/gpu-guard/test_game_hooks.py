from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
GAME_START = ROOT / "game-start.sh"
GAME_END = ROOT / "game-end.sh"


class GameHookOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "hooks.log"
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        self.env_file = self.root / "stack.env"
        self.env_file.write_text("TEST_ONLY=1\n", encoding="utf-8")
        self.guard = self.root / "guard"

        self.stub(
            "systemctl",
            """#!/usr/bin/env bash
set -eu
printf 'SYSTEMCTL %s\n' "$*" >>"$HOOK_LOG"
case "$*" in
  '--user is-active dominus-ai-stack.service')
    if [[ "${STACK_RACE:-0}" == 1 ]]; then printf 'activating\n'; exit 0; fi
    printf 'inactive\n'; exit 3 ;;
  '--user is-active --quiet vllm-server.service') exit 3 ;;
  *) exit 0 ;;
esac
""",
        )
        self.stub(
            "docker",
            """#!/usr/bin/env bash
set -eu
printf 'DOCKER %s\n' "$*" >>"$HOOK_LOG"
case "$*" in
  *'ps --all --quiet'*) printf '%064d\n' 0 ;;
  *'ps --status running --services'*) : ;;
esac
""",
        )
        self.stub(
            "mountpoint",
            "#!/usr/bin/env bash\nprintf 'MOUNTPOINT %s\n' \"$*\" >>\"$HOOK_LOG\"\nexit 0\n",
        )
        self.stub(
            "logger",
            "#!/usr/bin/env bash\nprintf 'LOGGER %s\n' \"$*\" >>\"$HOOK_LOG\"\n",
        )
        self.verifier = self.stub(
            "verifier",
            "#!/usr/bin/env bash\nprintf 'VERIFIER %s\n' \"$*\" >>\"$HOOK_LOG\"\n",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def stub(self, name: str, content: str) -> Path:
        path = self.bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
        return path

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.bin}:{environment['PATH']}",
                "HOOK_LOG": str(self.log),
                "DOMINUS_GPU_GUARD_TEST_MODE": "1",
                "DOMINUS_GPU_GUARD_DIR": str(self.guard),
                "DOMINUS_AI_SOURCE_DIR": str(self.source),
                "DOMINUS_AI_ENV_FILE": str(self.env_file),
                "DOMINUS_GPU_PROCESS_VERIFIER": str(self.verifier),
            }
        )
        return environment

    def run_hook(
        self, script: Path, extra_environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        environment = self.environment()
        environment.update(extra_environment or {})
        return subprocess.run(
            [str(script)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
        )

    def events(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def test_start_stops_unit_before_container_and_pid_verification(self) -> None:
        result = self.run_hook(GAME_START)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.guard / "game-active").is_file())
        events = self.events()
        unit_stop = events.index("SYSTEMCTL --user stop dominus-ai-stack.service")
        first_docker = next(index for index, event in enumerate(events) if event.startswith("DOCKER "))
        verifier = next(index for index, event in enumerate(events) if event.startswith("VERIFIER "))
        self.assertLess(unit_stop, first_docker)
        self.assertLess(unit_stop, verifier)

    def test_racing_unit_activation_fails_closed_before_docker(self) -> None:
        result = self.run_hook(GAME_START, {"STACK_RACE": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.guard / "game-active").is_file())
        self.assertFalse(any(event.startswith("DOCKER ") for event in self.events()))

    def test_end_restores_only_through_canonical_systemd_unit(self) -> None:
        self.guard.mkdir()
        (self.guard / "game-active").touch()
        result = self.run_hook(GAME_END)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.guard / "game-active").exists())
        events = self.events()
        guards = events.index(
            "SYSTEMCTL --user start vllm-idle-watchdog.service vllm-proxy.service"
        )
        stack = events.index("SYSTEMCTL --user start dominus-ai-stack.service")
        self.assertLess(guards, stack)
        self.assertFalse(any(event.startswith("DOCKER ") for event in events))


if __name__ == "__main__":
    unittest.main()
