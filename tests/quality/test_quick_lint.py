"""Preflight extracts only the hash-locked existing Ruff dependency."""

from pathlib import Path
import pytest
from tools.ci import quick_lint


def test_preflight_uses_exclusively_existing_ruff_hashes():
    root = Path(__file__).resolve().parents[2]
    selected = quick_lint.ruff_lock(
        (root / "docker/core/requirements-ci.txt").read_text()
    )
    assert selected.startswith("ruff==0.15.5")
    assert "--hash=sha256:" in selected
    assert "pytest==" not in selected


@pytest.mark.parametrize(
    "text", ["", "ruff==0.15.5\n", "ruff>=0.15\n", "ruff==0.15.5 --hash=sha256:bad\n"]
)
def test_incomplete_preflight_lock_is_rejected_before_install(text):
    with pytest.raises(ValueError):
        quick_lint.ruff_lock(text)


@pytest.mark.parametrize("failure", ["check", "format"])
def test_quick_lint_propagates_tool_failure(tmp_path, monkeypatch, failure):
    import subprocess

    root = Path(__file__).resolve().parents[2]
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        if failure in argv:
            raise subprocess.CalledProcessError(17, argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(quick_lint.subprocess, "run", command)
    with pytest.raises(subprocess.CalledProcessError) as error:
        quick_lint.run(root, tmp_path / "private")
    assert error.value.returncode == 17
    assert failure in calls[-1]
    if failure == "check":
        assert not any("format" in row for row in calls)
