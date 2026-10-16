"""Test-only keyring settings must preserve Chromium's namespace sandbox."""

import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("unsafe", ["--no-sandbox", "--no-sandbox=true"])
def test_disposable_flutter_chrome_keeps_namespace_sandbox(tmp_path, unsafe):
    profile = tmp_path / "flutter_tools.fixture/flutter_tools_chrome_device.fixture"
    profile.mkdir(parents=True, mode=0o700)
    binary, output = tmp_path / "chrome", tmp_path / "argv"
    binary.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE_CHROME_ARGV"\n')
    binary.chmod(0o700)
    result = subprocess.run(
        [
            str(ROOT / "flutter_app/tool/chrome_test.sh"),
            unsafe,
            f"--user-data-dir={profile}",
            "--remote-debugging-port=0",
            "about:blank",
        ],
        env={
            **os.environ,
            "TMPDIR": str(tmp_path),
            "KLUKAI_CHROME_BINARY": str(binary),
            "CAPTURE_CHROME_ARGV": str(output),
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    args = output.read_text().splitlines()
    assert not any(arg.startswith("--no-sandbox") for arg in args)
    assert "--disable-setuid-sandbox" in args
    assert "--password-store=basic" in args
    assert args[-3:] == [
        f"--user-data-dir={profile}",
        "--remote-debugging-port=0",
        "about:blank",
    ]


def test_existing_browser_profile_is_rejected_before_launch(tmp_path):
    marker = tmp_path / "launched"
    binary = tmp_path / "chrome"
    binary.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    binary.chmod(0o700)
    result = subprocess.run(
        [
            str(ROOT / "flutter_app/tool/chrome_test.sh"),
            f"--user-data-dir={tmp_path}/owner",
        ],
        env={
            **os.environ,
            "TMPDIR": str(tmp_path),
            "KLUKAI_CHROME_BINARY": str(binary),
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 64
    assert not marker.exists()
