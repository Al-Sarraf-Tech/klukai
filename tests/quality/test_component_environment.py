"""Component selection must preserve exclusive tools and bridge boundaries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from test_light_environment import (  # noqa: F401
    arguments,
    boundary,
    git,
    repository,
    subject,
)


def test_unknown_role_fails_before_creating_output(
    subject, repository, tmp_path, boundary
):
    boundary["root"] = repository
    args = arguments(repository, tmp_path)
    args.role = "subset"
    with pytest.raises(ValueError, match="role"):
        subject.run(args)
    assert not args.output.exists()
    assert not boundary["calls"]


def test_default_tools_record_fixed_role_and_lock(
    subject, repository, tmp_path, boundary
):
    boundary["root"] = repository
    args = arguments(repository, tmp_path)
    subject.run(args)
    report = json.loads((args.output / "manifest.json").read_text())
    assert report["role"] == "tools"
    assert report["lock"] == "docker/core/requirements-ci.txt"
    assert subject.selection(repository, args.output, args.revision) == str(
        args.output / "environment"
    )


def test_tools_consumer_rejects_manifest_claiming_bridge(
    subject, repository, tmp_path, boundary
):
    boundary["root"] = repository
    args = arguments(repository, tmp_path)
    subject.run(args)
    filename = args.output / "manifest.json"
    report = json.loads(filename.read_text())
    report["role"] = "bridge"
    report["lock"] = "docker/events-bridge/requirements-ci.txt"
    filename.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="role|lock"):
        subject.selection(repository, args.output, args.revision)


def test_component_prerequisite_failure_retains_evidence(
    tmp_path, monkeypatch, repository
):
    from tools.ci import component_environment as component

    monkeypatch.setattr(component.shutil, "which", lambda name: None)
    source = Path(component.__file__)
    target = repository / "tools/ci/component_environment.py"
    target.write_bytes(source.read_bytes())
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "component fixture")
    args = argparse.Namespace(
        root=repository,
        output=tmp_path / "private prerequisites",
        timeout=5.0,
        revision=git(repository, "rev-parse", "HEAD"),
    )
    with pytest.raises(ValueError, match="prerequisite"):
        component.prepare(args)
    result = json.loads((args.output / "manifest.json").read_text())
    assert result["passed"] is False
    assert "prerequisite" in result["error"]
    assert not (args.output / "docker").exists()


@pytest.fixture
def native_fixture(repository, tmp_path, monkeypatch):
    """Substitute only native command/download boundaries; execute real supervision."""
    import hashlib
    import shlex
    from tools.ci import component_environment as component
    from tools.ci import light_environment_sandbox as sandbox

    target = repository / component.HELPER
    target.write_bytes(Path(component.__file__).read_bytes())
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "component fixture")
    tools = tmp_path / "native tools"
    tools.mkdir()
    payload = '#!/bin/sh\nprintf "5.6.0\\n"\n'
    scripts = {
        "node": 'printf "v24.22.0\\n"',
        "npm": "node --version",
        "shellcheck": 'printf "ShellCheck fixture\\n"',
        "docker": 'if [ "$1" = --version ]; then echo Docker-fixture; else "$DOCKER_CONFIG/cli-plugins/docker-compose" version --short; fi',
        "curl": 'if [ "$1" = --version ]; then echo curl-fixture; else for arg do target="$arg"; done; printf %s '
        + shlex.quote(payload)
        + ' > "$target"; fi',
        "bwrap": 'if [ "$1" = --version ]; then echo bwrap-fixture; else /usr/bin/true; fi',
    }
    for name, script in scripts.items():
        path = tools / name
        path.write_text("#!/bin/sh\n" + script + "\n")
        path.chmod(0o700)
    monkeypatch.setattr(component.shutil, "which", lambda name: str(tools / name))
    monkeypatch.setattr(
        component, "COMPOSE_SHA256", hashlib.sha256(payload.encode()).hexdigest()
    )
    monkeypatch.setattr(sandbox, "BWRAP", tools / "bwrap")
    args = argparse.Namespace(
        root=repository,
        output=tmp_path / "private prerequisites",
        timeout=5.0,
        revision=git(repository, "rev-parse", "HEAD"),
    )
    return component, args, tools


def test_native_prerequisites_select_only_verified_private_compose(native_fixture):
    component, args, tools = native_fixture
    component.prepare(args)
    assert component.selection(args.root, args.output, args.revision) == str(
        args.output / "docker"
    )
    result = json.loads((args.output / "manifest.json").read_text())
    assert (
        result["tools"]["npm"]["version"] == "v24.22.0"
    )  # npm's env-node uses the pinned Node.
    assert result["sandbox"]["capability"] is True
    commands = json.loads((args.output / "commands.json").read_text())
    assert commands and all(
        row["supervisor"]["cleanup_complete"] is True for row in commands
    )
    assert set((args.output / "docker").iterdir()) == {
        args.output / "docker/cli-plugins"
    }


@pytest.mark.parametrize("fault", ["checksum", "namespace", "node", "download"])
def test_native_boundary_failure_never_selects_or_executes_unverified_compose(
    native_fixture, fault
):
    component, args, tools = native_fixture
    if fault == "checksum":
        component.COMPOSE_SHA256 = "0" * 64
    elif fault == "namespace":
        (tools / "bwrap").write_text(
            '#!/bin/sh\nif [ "$1" = --version ]; then echo fixture; else exit 42; fi\n'
        )
    elif fault == "node":
        (tools / "node").write_text("#!/bin/sh\necho v0.0.0\n")
    else:
        (tools / "curl").write_text(
            '#!/bin/sh\nif [ "$1" = --version ]; then echo fixture; else exit 42; fi\n'
        )
    with pytest.raises((ValueError, RuntimeError)):
        component.prepare(args)
    report = json.loads((args.output / "manifest.json").read_text())
    assert report["passed"] is False
    with pytest.raises(ValueError):
        component.selection(args.root, args.output, args.revision)
    records = json.loads((args.output / "commands.json").read_text())
    assert not any(
        row["argv"][0].endswith("/cli-plugins/docker-compose") for row in records
    )
    assert all(row["exit_code"] is not None for row in records)


@pytest.mark.parametrize(
    "damage", ["tool", "compose", "config", "source", "revision", "manifest"]
)
def test_component_selection_revalidates_all_materials(native_fixture, damage):
    component, args, tools = native_fixture
    component.prepare(args)
    if damage == "tool":
        (tools / "shellcheck").write_text("#!/bin/sh\nexit 0\n")
    elif damage == "compose":
        (args.output / "docker/cli-plugins/docker-compose").write_text(
            "#!/bin/sh\nexit 0\n"
        )
    elif damage == "config":
        (args.output / "docker/config.json").write_text("{}")
    elif damage == "source":
        (args.root / "README.md").write_text("mutation")
    elif damage == "revision":
        args.revision = "a" * 40
    else:
        (args.output / "manifest.json").write_text("{}")
    with pytest.raises(ValueError):
        component.selection(args.root, args.output, args.revision)


def test_export_does_not_open_environment_until_all_selections_pass(
    native_fixture, monkeypatch
):
    component, args, tools = native_fixture
    component.prepare(args)
    args.tools, args.bridge = (
        args.output / "tools-output",
        args.output / "bridge-output",
    )
    args.github_env = args.output / "github-env"
    args.github_env.write_text("KEEP=previous\n")

    def select(root, output, revision, *, role="tools"):
        if role == "bridge":
            raise ValueError("Rejected incomplete bridge closure")
        return str(args.output / "tools-environment")

    monkeypatch.setattr(component.light_environment, "selection", select)
    with pytest.raises(ValueError, match="bridge"):
        component.export(args)
    assert args.github_env.read_text() == "KEEP=previous\n"


@pytest.fixture
def bridge_repository(repository, tmp_path):
    import shutil
    from tools.ci import light_environment_role

    root = Path(__file__).resolve().parents[2]
    for name in light_environment_role.inputs("bridge"):
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "bridge inputs")
    # External pip boundary emits wheel/metadata for the selected bridge lock.
    probe_root = tmp_path / "external bridge material boundary"
    lock = probe_root / "docker/core/requirements-ci.txt"
    lock.parent.mkdir(parents=True)
    lock.write_bytes((root / light_environment_role.BRIDGE_LOCK).read_bytes())
    return repository, probe_root


def test_bridge_environment_is_complete_isolated_and_rejected_by_default_consumer(
    subject, bridge_repository, tmp_path, boundary
):
    repository, probe_root = bridge_repository
    boundary["root"] = probe_root
    args = arguments(repository, tmp_path)
    args.role = "bridge"
    subject.run(args)
    report = json.loads((args.output / "manifest.json").read_text())
    assert report["packages"]["pika"] == "1.3.2"
    assert report["packages"]["redis"] == "5.2.1"
    assert len(report["packages"]) == 11
    assert report["role"] == "bridge"
    assert subject.selection(
        repository, args.output, args.revision, role="bridge"
    ) == str(args.output / "environment")
    with pytest.raises(ValueError, match="role|lock"):
        subject.selection(repository, args.output, args.revision)


@pytest.mark.parametrize(
    "mode", ["missing", "extra", "duplicate", "wrong", "corrupt-wheel", "check-fails"]
)
def test_bridge_incomplete_or_failed_closure_is_never_selected(
    subject, bridge_repository, tmp_path, boundary, mode
):
    repository, probe_root = bridge_repository
    boundary.update(root=probe_root, mode=mode)
    args = arguments(repository, tmp_path)
    args.role = "bridge"
    with pytest.raises(ValueError):
        subject.run(args)
    assert json.loads((args.output / "manifest.json").read_text())["passed"] is False
    with pytest.raises(ValueError):
        subject.selection(repository, args.output, args.revision, role="bridge")


def test_complete_tools_manifest_cannot_be_selected_as_bridge(
    subject, bridge_repository, tmp_path, boundary
):
    repository, probe_root = bridge_repository
    boundary["root"] = repository
    args = arguments(repository, tmp_path)
    subject.run(args)
    with pytest.raises(ValueError, match="role|lock"):
        subject.selection(repository, args.output, args.revision, role="bridge")


def test_bridge_subset_lock_fails_before_python_children(
    subject, bridge_repository, tmp_path, boundary
):
    repository, probe_root = bridge_repository
    from tools.ci import light_environment_role

    path = repository / light_environment_role.BRIDGE_LOCK
    path.write_text("pika==1.3.2 \\\n    --hash=sha256:" + "a" * 64 + "\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "incomplete fixture lock")
    boundary["root"] = probe_root
    args = arguments(repository, tmp_path)
    args.role = "bridge"
    with pytest.raises(ValueError):
        subject.run(args)
    assert not boundary["calls"]


def test_native_selection_rejects_retargeted_command_alias(native_fixture, monkeypatch):
    component, args, tools = native_fixture
    component.prepare(args)
    alternative = tools / "different-shellcheck"
    alternative.write_text("#!/bin/sh\nexit 0\n")
    alternative.chmod(0o700)
    monkeypatch.setattr(
        component.shutil,
        "which",
        lambda name: str(alternative if name == "shellcheck" else tools / name),
    )
    with pytest.raises(ValueError, match="prerequisite"):
        component.selection(args.root, args.output, args.revision)


@pytest.mark.parametrize("complete", [True, False])
def test_component_workflow_pytest_starts_with_strict_warnings_and_explicit_loop_defaults(
    tmp_path,
    complete,
):
    import os
    import subprocess
    import sys
    import yaml

    root = Path(__file__).resolve().parents[2]
    job = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"][
        "components"
    ]
    step = next(row for row in job["steps"] if "make test-ops" in row.get("run", ""))
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    sample = tmp_path / "test_async_fixture.py"
    sample.write_text(
        "import pytest\n@pytest.mark.asyncio\nasync def test_loop():\n    assert 3 + 4 == 7\n"
    )
    environment = {
        **os.environ,
        **job["env"],
        **step["env"],
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    if not complete:
        environment.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        [
            sys.executable,
            "-Werror",
            "-m",
            "pytest",
            "-p",
            "pytest_asyncio.plugin",
            "-c",
            str(config),
            "-q",
            str(sample),
        ],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert (result.returncode == 0) is complete, result.stdout + result.stderr
    if complete:
        assert "1 passed" in result.stdout
    else:
        assert "asyncio_default_fixture_loop_scope" in result.stdout + result.stderr
