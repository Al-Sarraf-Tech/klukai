"""A failed Flutter build must never replace the served PWA with stale output."""

import os
from pathlib import Path
import shutil
import subprocess


def test_failed_flutter_build_preserves_served_bundle(tmp_path):
    root = Path(__file__).resolve().parents[2]
    shutil.copyfile(root / "Makefile", tmp_path / "Makefile")
    stale = tmp_path / "flutter_app/build/web"
    stale.mkdir(parents=True)
    (stale / "index.html").write_text("stale build")
    served = tmp_path / "web-build"
    served.mkdir()
    (served / "index.html").write_text("working release")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    flutter = binaries / "flutter"
    flutter.write_text("#!/bin/sh\nexit 42\n")
    flutter.chmod(0o755)
    result = subprocess.run(
        ["make", "build-pwa"],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "SKIP_PWA": "0"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0, result.stdout
    assert (served / "index.html").read_text() == "working release"
