"""Export must bind actual installed bytes as well as installation identities."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
import yaml

from test_light_environment import ROOT, arguments

from test_light_environment import boundary as boundary
from test_light_environment import repository as repository
from test_light_environment import subject as subject


def export_action(tmp_path, repository, args):
    script = yaml.safe_load(
        (ROOT / ".github/actions/python-tools/action.yml").read_text()
    )["runs"]["steps"][0]["run"]
    tools = tmp_path / "boundary tools"
    tools.mkdir()
    python = tools / "python3"
    python.write_text(
        '#!/bin/sh\nif [ "$1" = -m ]; then exit 0; fi\nexec "$REAL_PYTHON" "$@"\n'
    )
    python.chmod(0o700)
    selected, exported = tmp_path / "github-path", tmp_path / "github-env"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
        env={
            **os.environ,
            "PATH": str(tools) + os.pathsep + os.environ["PATH"],
            "REAL_PYTHON": sys.executable,
            "GITHUB_WORKSPACE": str(repository),
            "KLUKAI_CI_OUTPUT": str(args.output),
            "KLUKAI_CI_REVISION": args.revision,
            "GITHUB_PATH": str(selected),
            "GITHUB_ENV": str(exported),
            "PYTHONOPTIMIZE": "2",
        },
    )
    return result, selected, exported


@pytest.mark.parametrize(
    "damage",
    [
        "missing-python",
        "replaced-python",
        "changed-tool",
        "deleted-tool",
        "extra-file",
        "executable-mode",
        "retarget-python",
    ],
)
def test_exact_action_rejects_installed_bytes_despite_unchanged_package_metadata(
    subject, repository, tmp_path, boundary, damage
):
    boundary["root"] = repository
    if damage == "retarget-python":
        boundary["mode"] = "linked-interpreter"
    args = arguments(repository, tmp_path)
    subject.run(args)
    before = (args.output / "manifest.json").read_bytes()
    interpreter = args.output / "environment/bin/python"
    tool = args.output / "environment/lib/site-packages/ruff/__main__.py"
    metadata = tool.parent.parent / "ruff.dist-info/METADATA"
    metadata_before = metadata.read_bytes()
    if damage == "missing-python":
        interpreter.unlink()
    elif damage == "replaced-python":
        interpreter.unlink()
        interpreter.write_text("#!/bin/sh\nprintf modified-interpreter\\n\n")
        interpreter.chmod(0o700)
    elif damage == "changed-tool":
        tool.write_text("raise SystemExit('modified tool')\n")
    elif damage == "deleted-tool":
        tool.unlink()
    elif damage == "extra-file":
        (tool.parent / "unlocked.py").write_text("changed")
    elif damage == "retarget-python":
        interpreter.unlink()
        interpreter.symlink_to("/usr/bin/true")
    else:
        interpreter.chmod(0o600)
    assert metadata.read_bytes() == metadata_before
    assert (args.output / "manifest.json").read_bytes() == before
    result, selected, exported = export_action(tmp_path, repository, args)
    assert result.returncode != 0, (
        "Changed/missing installed bytes must reject selection"
    )
    assert not selected.exists() and not exported.exists()


def test_legitimate_venv_interpreter_link_is_bound_and_exportable(
    subject, repository, tmp_path, boundary
):
    boundary.update(root=repository, mode="linked-interpreter")
    args = arguments(repository, tmp_path)
    subject.run(args)
    assert (args.output / "environment/bin/python").is_symlink()
    result, selected, exported = export_action(tmp_path, repository, args)
    assert result.returncode == 0, result.stderr
    assert selected.exists() and exported.exists()


def test_unavailable_pidfd_prerequisite_fails_before_children_or_action_exports(
    subject, repository, tmp_path, boundary, monkeypatch
):
    boundary["root"] = repository
    args = arguments(repository, tmp_path)

    def unavailable(_pid, _flags=0):
        raise PermissionError("fixture: pidfd unavailable")

    monkeypatch.setattr(subject.light_environment_process.os, "pidfd_open", unavailable)
    with pytest.raises(PermissionError):
        subject.run(args)
    assert not boundary["calls"]
    result, selected, exported = export_action(tmp_path, repository, args)
    assert result.returncode != 0
    assert not selected.exists() and not exported.exists()
