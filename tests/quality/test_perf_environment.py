"""The perf harness gets its complete fixed closure, never the general tools role."""
# Imported pytest fixtures intentionally share the names of test parameters.
# ruff: noqa: F811

import json
from pathlib import Path
import re
import shutil

import pytest

from test_light_environment import (  # noqa: F401
    arguments,
    boundary,
    git,
    repository,
    subject,
)

from tools.ci import light_environment_role as roles

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = {
    "anyio",
    "certifi",
    "h11",
    "httpcore",
    "httpx",
    "idna",
    "pip",
    "psycopg",
    "psycopg-binary",
    "redis",
    "typing-extensions",
}


def test_perf_role_uses_exact_existing_ci_versions_and_complete_harness_closure():
    expected = roles.expected(ROOT, "perf")
    assert set(expected) == PACKAGES
    complete = roles.expected(ROOT, "tools")
    assert expected == {name: complete[name] for name in PACKAGES}
    assert set(roles.inputs("perf")) == {
        "docker/core/requirements-perf.in",
        "docker/core/requirements-perf.txt",
    }


def test_perf_material_hashes_are_copied_unchanged_from_ci_lock():
    def blocks(path):
        return {
            match[1]: match[0]
            for match in re.finditer(
                r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==[^\n]*(?:\n[ \t]+[^\n]*)*",
                path.read_text(),
                re.MULTILINE,
            )
        }

    complete = blocks(ROOT / roles.TOOLS_LOCK)
    perf = blocks(ROOT / roles.PERF_LOCK)
    assert perf == {name: complete[name] for name in PACKAGES}


@pytest.fixture
def perf_repository(repository, tmp_path):
    for name in roles.inputs("perf"):
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "perf inputs")
    probe_root = tmp_path / "external perf material boundary"
    lock = probe_root / "docker/core/requirements-ci.txt"
    lock.parent.mkdir(parents=True)
    lock.write_bytes((ROOT / roles.lock("perf")).read_bytes())
    return repository, probe_root


def test_perf_environment_is_complete_and_rejected_by_general_tools_consumer(
    subject,
    perf_repository,
    tmp_path,
    boundary,
):
    root, probe = perf_repository
    boundary["root"] = probe
    args = arguments(root, tmp_path)
    args.role = "perf"
    subject.run(args)
    report = json.loads((args.output / "manifest.json").read_text())
    assert set(report["packages"]) == PACKAGES
    assert report["role"] == "perf"
    assert subject.selection(root, args.output, args.revision, role="perf") == str(
        args.output / "environment"
    )
    with pytest.raises(ValueError, match="role|lock"):
        subject.selection(root, args.output, args.revision)


@pytest.mark.parametrize(
    "mode", ["missing", "extra", "wrong", "corrupt-wheel", "check-fails"]
)
def test_perf_incomplete_or_corrupt_environment_is_never_selected(
    subject,
    perf_repository,
    tmp_path,
    boundary,
    mode,
):
    root, probe = perf_repository
    boundary.update(root=probe, mode=mode)
    args = arguments(root, tmp_path)
    args.role = "perf"
    with pytest.raises(ValueError):
        subject.run(args)
    with pytest.raises(ValueError):
        subject.selection(root, args.output, args.revision, role="perf")


def test_perf_pin_drift_is_rejected_before_running_python_children(
    subject,
    perf_repository,
    tmp_path,
    boundary,
):
    root, probe = perf_repository
    path = root / roles.lock("perf")
    path.write_text(path.read_text().replace("httpx==0.28.1", "httpx==0.28.0"))
    git(root, "add", ".")
    git(root, "commit", "-qm", "drifted perf fixture")
    boundary["root"] = probe
    args = arguments(root, tmp_path)
    args.role = "perf"
    with pytest.raises(ValueError, match="pins"):
        subject.run(args)
    assert not boundary["calls"]
