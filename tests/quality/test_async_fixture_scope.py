"""The legacy session fixture retains its loop under explicit CI defaults."""

import os
from pathlib import Path
import subprocess
import sys


def test_session_integration_fixture_keeps_its_own_loop(tmp_path):
    core = Path(__file__).resolve().parents[2] / "docker/core"
    test = tmp_path / "test_fixture_scope.py"
    test.write_text(
        "from tests.integration.conftest import (\n"
        "    _create_test_user, test_user_id, test_password,\n"
        ")\n"
        "def test_session_fixture(_create_test_user):\n"
        "    assert _create_test_user is None\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error",
            "-m",
            "pytest",
            "-p",
            "pytest_asyncio.plugin",
            "-c",
            str(core / "pytest.ini"),
            "-q",
            "-W",
            "error",
            str(test),
        ],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "HOME": str(tmp_path),
            "PYTHONPATH": str(core),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "DATABASE_URL": "postgresql://disabled:disabled@127.0.0.1:1/disabled",
            "REDIS_URL": "redis://127.0.0.1:1/0",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
