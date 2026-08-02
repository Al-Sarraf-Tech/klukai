from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("verify-canonical-gpu-processes.py")
SPEC = importlib.util.spec_from_file_location("verify_canonical_gpu_processes", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)


class CanonicalGpuProcessTests(unittest.TestCase):
    def proc(self, root: Path, pid: int, command: str, cgroup: str) -> None:
        process = root / str(pid)
        process.mkdir()
        (process / "cmdline").write_bytes(command.replace(" ", "\0").encode() + b"\0")
        (process / "cgroup").write_text(cgroup, encoding="utf-8")

    def test_desktop_and_game_compute_are_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.proc(root, 10, "/usr/bin/kwin_wayland", "0::/user.slice")
            self.proc(root, 11, "/games/example/game.exe", "0::/user.slice")
            rows = "10, kwin_wayland, 180\n11, game.exe, 2048\n"
            self.assertEqual(guard.canonical_processes(rows, (), root), [])

    def test_known_ai_commands_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.proc(root, 20, "python /opt/ComfyUI/main.py", "0::/user.slice")
            self.proc(root, 21, "python -m vllm.entrypoints.openai.api_server", "0::/user.slice")
            rows = "20, python, 12000\n21, python, 22000\n"
            result = guard.canonical_processes(rows, (), root)
            self.assertEqual([process.pid for process in result], [20, 21])

    def test_canonical_container_cgroup_is_rejected_even_for_generic_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            container_id = "a" * 64
            self.proc(
                root,
                30,
                "python -m uvicorn app.main:app",
                f"0::/system.slice/docker-{container_id}.scope",
            )
            result = guard.canonical_processes(
                "30, python, 4096\n", (container_id,), root
            )
            self.assertEqual([process.pid for process in result], [30])

    def test_disappearing_process_and_bad_nvidia_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(guard.canonical_processes("40, python, 100\n", (), root), [])
            with self.assertRaisesRegex(ValueError, "unparseable"):
                guard.parse_compute_csv("not,a,valid,pid")


if __name__ == "__main__":
    unittest.main()
