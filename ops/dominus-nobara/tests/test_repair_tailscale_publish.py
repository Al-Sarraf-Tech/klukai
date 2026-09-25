from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/repair-tailscale-publish.sh"


class RepairTailscalePublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.marker = self.root / "game-active"
        self.stub("logger", "#!/usr/bin/env bash\nexit 0\n")
        self.stub("mountpoint", "#!/usr/bin/env bash\nexit 0\n")
        self.stub("docker", "#!/usr/bin/env bash\nexit 0\n")
        self.stub(
            "systemctl",
            "#!/usr/bin/env bash\nprintf 'inactive\\n'\nexit 3\n",
        )
        self.stub(
            "tailscale",
            "#!/usr/bin/env bash\nprintf '100.107.121.5\\n'\n",
        )
        self.stub(
            "ss",
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' '100.107.121.5:1234' '100.107.121.5:8301' '100.107.121.5:8390'\n",
        )
        self.stub("sleep", "#!/usr/bin/env bash\nexit 0\n")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def stub(self, name: str, content: str) -> None:
        path = self.bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def run_script(self, *args: str, marker: bool = False, dry_run: bool = True) -> str:
        if marker:
            self.marker.write_text("game\n", encoding="utf-8")
        elif self.marker.exists():
            self.marker.unlink()
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.bin}:{environment.get('PATH', '')}",
                "DOMINUS_PUBLISH_REPAIR_DRY_RUN": "1" if dry_run else "0",
                "DOMINUS_GAME_MARKER": str(self.marker),
                "DOMINUS_TAILSCALE_WAIT_SECONDS": "3",
            }
        )
        completed = subprocess.run(
            ["bash", str(SCRIPT), *args],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def test_game_marker_blocks_repair_even_when_ports_are_closed(self) -> None:
        self.stub("ss", "#!/usr/bin/env bash\nexit 0\n")
        self.assertEqual(self.run_script(marker=True), "noop-game")

    def test_missing_tailscale_address_does_not_start_containers(self) -> None:
        self.stub("tailscale", "#!/usr/bin/env bash\nexit 0\n")
        self.stub("ss", "#!/usr/bin/env bash\nexit 0\n")
        self.assertEqual(self.run_script(), "noop-no-ip")

    def test_healthy_listeners_are_left_alone(self) -> None:
        self.assertEqual(self.run_script(), "noop-healthy")

    def test_port_prefix_does_not_count_as_a_healthy_publish(self) -> None:
        self.stub(
            "ss",
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' 'LISTEN 0 4096 100.107.121.5:12340 0.0.0.0:*' "
            "'LISTEN 0 4096 100.107.121.5:8301 0.0.0.0:*' "
            "'LISTEN 0 4096 100.107.121.5:8390 0.0.0.0:*'\n",
        )
        self.assertEqual(self.run_script(), "repair lmstudio-compat")

    def test_game_start_during_checks_prevents_recreate(self) -> None:
        self.stub("ss", "#!/usr/bin/env bash\nexit 0\n")
        self.stub(
            "flock",
            '#!/usr/bin/env bash\ntouch "$DOMINUS_GAME_MARKER"\n',
        )
        self.stub(
            "docker",
            '#!/usr/bin/env bash\n[[ "$1" == info ]] && exit 0\n'
            'echo "unexpected container mutation" >&2\nexit 99\n',
        )
        self.assertEqual(self.run_script(dry_run=False), "")

    def test_stack_units_own_execstart_repairs_while_activating(self) -> None:
        self.stub(
            "systemctl",
            "#!/usr/bin/env bash\nprintf 'activating\\n'\nexit 0\n",
        )
        self.stub("ss", "#!/usr/bin/env bash\nexit 0\n")
        self.assertEqual(
            self.run_script("--from-unit"),
            "repair lmstudio-compat companion-voice speaches",
        )

    def test_activating_stack_is_not_recreated(self) -> None:
        self.stub(
            "systemctl",
            "#!/usr/bin/env bash\nprintf 'activating\\n'\nexit 0\n",
        )
        self.stub("ss", "#!/usr/bin/env bash\nexit 0\n")
        self.assertEqual(self.run_script(), "noop-activating")

    def test_closed_gateway_recreates_only_that_service(self) -> None:
        self.stub(
            "ss",
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' '100.107.121.5:8301' '100.107.121.5:8390'\n",
        )
        self.assertEqual(self.run_script(), "repair lmstudio-compat")

    def test_all_closed_publishes_recreate_in_port_order(self) -> None:
        self.stub("ss", "#!/usr/bin/env bash\nexit 0\n")
        self.assertEqual(
            self.run_script(),
            "repair lmstudio-compat companion-voice speaches",
        )

    def test_raid_or_docker_down_is_a_no_op(self) -> None:
        self.stub("mountpoint", "#!/usr/bin/env bash\nexit 1\n")
        self.assertEqual(self.run_script(), "noop-no-raid")
        self.stub("mountpoint", "#!/usr/bin/env bash\nexit 0\n")
        self.stub("docker", "#!/usr/bin/env bash\nexit 1\n")
        self.assertEqual(self.run_script(), "noop-no-docker")

    def test_wait_for_address_succeeds_when_the_ip_is_already_up(self) -> None:
        self.assertEqual(self.run_script("--wait-for-address"), "")

    def test_wait_for_address_continues_when_the_ip_never_appears(self) -> None:
        self.stub("tailscale", "#!/usr/bin/env bash\nexit 1\n")
        self.assertEqual(self.run_script("--wait-for-address"), "")


if __name__ == "__main__":
    unittest.main()
