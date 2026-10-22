"""Absence evidence is a successful complete listing, never failed inspect output.

Compile only the target method with a fake subprocess dependency. No Docker,
SSH, controller/module initialization, CLI, or network operation can execute.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/klukai-release.py"
ID_A, ID_B = "a" * 64, "b" * 64
NAME = "companion-core"


def observed_absence(output, *, returncode=0, error=None):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        assert argv == ["docker", "container", "ls", "--all", "--no-trunc", "--format", "{{.ID}} {{.Names}}"]
        assert kwargs == {"capture_output": True, "text": True, "timeout": 30, "check": False}
        if error is not None:
            raise error
        return SimpleNamespace(returncode=returncode, stdout=output, stderr="fake diagnostic")

    module = ast.parse(SCRIPT.read_text())
    target = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "Target")
    method = next(node for node in target.body if isinstance(node, ast.FunctionDef) and node.name == "container_absent")
    namespace = {"subprocess": SimpleNamespace(run=fake_run), "ReleaseError": RuntimeError, "re": re}
    code = compile(ast.Module(body=[method], type_ignores=[]), str(SCRIPT), "exec")
    exec(code, namespace)  # noqa: S102 - repository method AST with fake subprocess only
    answer = namespace["container_absent"](SimpleNamespace(container=NAME))
    assert len(calls) == 1
    return answer


@pytest.mark.parametrize("output, expected", [
    ("", True),
    (f"{ID_A} unrelated\n", True),
    (f"{ID_A} {NAME}-old\n", True),
    (f"{ID_A} {NAME}\n", False),
    (f"{ID_A} unrelated\n{ID_B} {NAME}\n", False),
    (f"{ID_A} unrelated,linked/alias\n", True),
    (f"{ID_A} {NAME},linked/alias\n", False),
    (f"{ID_A} unrelated,linked/{NAME}\n", True),
    (f"{ID_A} linked/alias,{NAME}\n", False),
])
def test_only_a_successful_validated_listing_can_confirm_absence(output, expected):
    assert observed_absence(output) is expected


@pytest.mark.parametrize("output", [
    "\n", " \n", "not-a-list", None,
    f"short {NAME}\n", f"{'G' * 64} unrelated\n", f"{ID_A}\n",
    f"{ID_A} bad/name\n", f"{ID_A} -bad-name\n", f"{ID_A} name extra\n",
    f"{ID_A} first\n{ID_A} second\n", f"{ID_A} same\n{ID_B} same\n",
    f"{ID_A} {NAME}\nmalformed\n",
    f"{ID_A} first,second\n", f"{ID_A} first,first\n",
    f"{ID_A} first,linked/alias,linked/alias\n",
    f"{ID_A} first,linked/alias\n{ID_B} second,linked/alias\n",
    f"{ID_A} first,linked//alias\n", f"{ID_A} first,/alias\n",
    f"{ID_A} first,\n",
])
def test_malformed_or_duplicate_listing_data_cannot_confirm_absence(output):
    with pytest.raises(RuntimeError):
        observed_absence(output)


@pytest.mark.parametrize("output", ["", f"{ID_A} unrelated\n"])
def test_a_daemon_or_access_failure_cannot_confirm_absence(output):
    with pytest.raises(RuntimeError):
        observed_absence(output, returncode=1)


def test_a_timed_out_listing_remains_unknown():
    with pytest.raises(TimeoutError):
        observed_absence("", error=TimeoutError("fake listing timed out"))
