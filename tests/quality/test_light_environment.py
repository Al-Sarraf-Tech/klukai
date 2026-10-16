"""Native light CI must reject incomplete tools before exporting any environment."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZipFile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def subject():
    from tools.ci import light_environment

    return light_environment


@pytest.fixture
def repository(tmp_path, subject):
    root = tmp_path / "source with spaces"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.hooksPath", "/dev/null")
    for name in subject.INPUTS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, path)
    (root / "README.md").write_text("bound source\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture")
    return root


def arguments(repository, tmp_path):
    return argparse.Namespace(
        root=repository,
        output=tmp_path / "private tools output",
        revision=git(repository, "rev-parse", "HEAD"),
        timeout=10.0,
    )


@pytest.fixture
def boundary(monkeypatch, subject):
    """Only substitute external Python/pip processes; Git/files/parsers stay real."""
    actual = subprocess.Popen
    state = {"mode": "ok", "calls": [], "root": None}

    def launch(argv, **kwargs):
        if "-C" in argv or argv[0] == "bash":  # Git and exact shell controls stay real.
            return actual(argv, **kwargs)
        prefix = []
        if len(argv) > 4 and str(argv[2]).endswith("/light_environment_supervisor.py"):
            prefix, argv = argv[:5], argv[5:]

        def execute(command):
            return actual([*prefix, *command], **kwargs)

        state["calls"].append(list(argv))
        if argv[0] == "/usr/bin/bwrap":
            if "--version" in argv:
                return execute(argv)
            assert "--unshare-all" in argv and "--clearenv" in argv
            assert "--unshare-net" not in argv and "--share-net" not in argv
            assert not any(
                argv[i : i + 3] == ["--ro-bind", "/", "/"] for i in range(len(argv))
            )
            argv = argv[argv.index("--") + 1 :]
            # This external-boundary stub substitutes bwrap and its namespace
            # init; the actual init lifecycle is tested with real subprocesses.
            argv = argv[argv.index("--") + 1 :]
            if argv == ["/usr/bin/true"]:
                if state["mode"] == "sandbox-fails":
                    return actual(
                        [sys.executable, "-I", "-c", "raise SystemExit(42)"], **kwargs
                    )
                return execute(argv)
        env = kwargs["env"]
        assert env.get("PIP_CONFIG_FILE") == "/dev/null"
        assert "PIP_INDEX_URL" not in env and "PYTHONPATH" not in env
        assert "ANTHROPIC_API_KEY" not in env
        code = ""
        if "-c" in argv and "klukai_builder_ca" in argv[-1]:
            candidates = [
                Path(value)
                for value in (
                    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
                    "/etc/ssl/certs/ca-certificates.crt",
                    "/etc/ssl/ca-bundle.pem",
                )
            ]
            selected = next(path for path in candidates if path.is_file())
            if state.get("ca_bundle"):
                import shutil

                selected_bundle = (
                    Path(argv[0]).parent.parent
                    / "lib/site-packages/pip/_vendor/certifi/cacert.pem"
                )
                selected_bundle.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(selected, selected_bundle)
                selected = selected_bundle
            return execute(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    "print("
                    + repr(json.dumps({"klukai_builder_ca": str(selected)}))
                    + ")",
                ]
            )
        if "-c" in argv and "importlib.metadata" not in argv[-1]:
            return execute(argv)  # Real native interpreter probe.
        if "venv" in argv:
            folder = Path(argv[-1]) / "bin"
            folder.mkdir(parents=True)
            (folder / "python").write_text("fixture interpreter boundary")
            (folder / "python").chmod(0o700)
            if state["mode"] == "linked-interpreter" and "--without-pip" in argv:
                (folder / "python").unlink()
                (folder / "python").symlink_to(Path(sys.executable).resolve())
        elif "-c" in argv:
            lock = state["root"] / "docker/core/requirements-ci.txt"
            import re

            rows = [
                [m[1].lower().replace("_", "-"), m[2]]
                for m in re.finditer(
                    r"(?m)^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^\s]+)", lock.read_text()
                )
            ]
            if state["mode"] == "missing":
                rows.pop()
            elif state["mode"] == "extra":
                rows.append(["unlocked", "1"])
            elif state["mode"] == "duplicate":
                rows.append(rows[0])
            elif state["mode"] == "wrong":
                rows[0][1] = "0"
            code = "print(" + repr(json.dumps({"packages": rows})) + ")"
        elif "--version" in argv:
            code = "print('pip fixture boundary')"
        elif "download" in argv:
            assert "--index-url=https://pypi.org/simple" in argv
            assert all(
                flag in argv
                for flag in ("--require-hashes", "--no-deps", "--no-build-isolation")
            )
            if state["mode"] == "corrupt-download":
                code = "import sys; print('hash mismatch',file=sys.stderr); sys.exit(1)"
        elif "wheel" in argv:
            assert all(
                flag in argv
                for flag in (
                    "--no-index",
                    "--no-deps",
                    "--no-build-isolation",
                    "--require-hashes",
                )
            )
            target = Path(argv[argv.index("--wheel-dir") + 1])
            import re

            lock = state["root"] / "docker/core/requirements-ci.txt"
            for match in re.finditer(
                r"(?m)^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^\s]+)", lock.read_text()
            ):
                name, version = match.groups()
                with ZipFile(
                    target / f"{name}-{version}-py3-none-any.whl", "w"
                ) as wheel:
                    wheel.writestr(
                        f"{name}-{version}.dist-info/METADATA",
                        f"Name: {name}\nVersion: {version}\n",
                    )
            if state["mode"] == "corrupt-wheel":
                next(target.glob("*.whl")).write_bytes(b"invalid zip")
        elif "install" in argv:
            if "--python" in argv:
                environment = Path(argv[argv.index("--python") + 1]).parent.parent
                tools = environment / "lib/site-packages/ruff"
                tools.mkdir(parents=True)
                (tools / "__main__.py").write_text("print('fixture tool')\n")
                metadata = environment / "lib/site-packages/ruff.dist-info"
                metadata.mkdir()
                (metadata / "METADATA").write_text("Name: ruff\nVersion: 0.15.7\n")
            assert "--require-hashes" in argv and "--no-deps" in argv
            if "--no-index" not in argv:
                assert "--index-url=https://pypi.org/simple" in argv
        elif "check" in argv:
            if state["mode"] == "check-fails":
                code = "raise SystemExit(1)"
        else:
            raise AssertionError(f"Unexpected process: {argv}")
        if state["mode"] == "moves-source" and "download" in argv:
            (state["root"] / "README.md").write_text("changed")
        if state["mode"] == "moves-head" and "download" in argv:
            git(state["root"], "commit", "--allow-empty", "-qm", "moved")
        return execute([sys.executable, "-I", "-c", code])

    monkeypatch.setattr(subject.subprocess, "Popen", launch)
    return state


def test_complete_native_bootstrap_retains_evidence_before_selecting_tools(
    subject, repository, tmp_path, boundary
):
    boundary["root"] = repository
    args = arguments(repository, tmp_path)
    subject.run(args)
    report = json.loads((args.output / "manifest.json").read_text())
    assert report["passed"] is True and report["source_unchanged"] is True
    assert len(report["packages"]) == 131
    assert report["host"]["executable"] == str(Path(sys.executable).resolve())
    assert len(report["host"]["sha256"]) == 64
    assert args.output.stat().st_mode & 0o777 == 0o700
    assert json.loads((args.output / "commands.json").read_text())
    assert (
        len(json.loads((args.output / "wheel-materials.json").read_text())["wheels"])
        == 131
    )


@pytest.mark.parametrize(
    "mode",
    [
        "missing",
        "extra",
        "duplicate",
        "wrong",
        "corrupt-download",
        "corrupt-wheel",
        "check-fails",
        "moves-source",
        "moves-head",
        "sandbox-fails",
    ],
)
def test_failed_bootstrap_retains_failed_manifest(
    subject, repository, tmp_path, boundary, mode
):
    boundary.update(root=repository, mode=mode)
    args = arguments(repository, tmp_path)
    with pytest.raises((ValueError, RuntimeError, OSError)):
        subject.run(args)
    result = json.loads((args.output / "manifest.json").read_text())
    assert result["passed"] is False
    assert "error" in result
    if mode == "corrupt-download":
        assert not any("wheel" in call for call in boundary["calls"])


@pytest.mark.parametrize(
    "damage",
    ["dirty", "linked-root", "linked-material", "wrong-revision", "existing-output"],
)
def test_source_or_output_rejection_precedes_python_children(
    subject, repository, tmp_path, boundary, damage
):
    boundary["root"] = repository
    args = arguments(repository, tmp_path)
    if damage == "dirty":
        (repository / "untracked").write_text("dirty")
    elif damage == "linked-root":
        alias = tmp_path / "source alias"
        alias.symlink_to(repository, target_is_directory=True)
        args.root = alias
    elif damage == "linked-material":
        target = repository / "docker/core/requirements-ci.txt"
        original = tmp_path / "lock"
        target.rename(original)
        target.symlink_to(original)
    elif damage == "wrong-revision":
        args.revision = "a" * 40
    else:
        args.output.mkdir()
        (args.output / "keep").write_text("prior evidence")
    with pytest.raises((ValueError, OSError)):
        subject.run(args)
    assert not boundary["calls"]
    if damage == "existing-output":
        assert (args.output / "keep").read_text() == "prior evidence"


@pytest.mark.parametrize("kind", ["failure", "timeout", "signal"])
def test_owned_children_are_reaped_with_private_command_evidence(
    subject, tmp_path, kind
):
    commands = subject.Commands(tmp_path, 0.15)
    code = "raise SystemExit(7)" if kind == "failure" else "import time; time.sleep(30)"
    if kind == "signal":
        code = "import os, signal; os.kill(os.getppid(),signal.SIGTERM)"
    with pytest.raises((ValueError, RuntimeError, subprocess.SubprocessError)):
        with subject.cancellation_scope():
            commands.run([sys.executable, "-I", "-c", code])
    records = json.loads((tmp_path / "commands.json").read_text())
    assert records[0]["exit_code"] is not None
    assert Path(tmp_path / records[0]["stderr"]).exists()
    with pytest.raises(ProcessLookupError):
        os.kill(records[0]["pid"], 0)


@pytest.mark.parametrize(
    "valid,optimization", [(True, "0"), (True, "2"), (False, "0"), (False, "2")]
)
def test_action_exports_only_a_valid_complete_success(
    tmp_path, valid, optimization, subject, repository, boundary
):
    action = ROOT / ".github/actions/python-tools/action.yml"
    script = yaml.safe_load(action.read_text())["runs"]["steps"][0]["run"]
    boundary["root"] = repository
    args = arguments(repository, tmp_path)
    subject.run(args)
    output = args.output
    filename = output / "manifest.json"
    manifest = json.loads(filename.read_text())
    manifest["passed"] = valid
    filename.write_text(json.dumps(manifest))
    tools = tmp_path / "boundary tools"
    tools.mkdir()
    python = tools / "python3"
    python.write_text(
        '#!/bin/sh\nif [ "$1" = -m ]; then exit 0; fi\nexec "$REAL_PYTHON" "$@"\n'
    )
    python.chmod(0o700)
    selected = tmp_path / "selected path"
    exported = tmp_path / "selected env"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": str(tools) + os.pathsep + os.environ["PATH"],
            "REAL_PYTHON": sys.executable,
            "GITHUB_WORKSPACE": str(repository),
            "KLUKAI_CI_OUTPUT": str(output),
            "KLUKAI_CI_REVISION": args.revision,
            "GITHUB_PATH": str(selected),
            "GITHUB_ENV": str(exported),
            "PYTHONOPTIMIZE": optimization,
        },
    )
    assert (result.returncode == 0) is valid, result.stderr
    if valid:
        assert selected.read_text() == str(output / "environment/bin") + "\n"
        assert (
            exported.read_text() == "VIRTUAL_ENV=" + str(output / "environment") + "\n"
        )
    else:
        assert not selected.exists() and not exported.exists()


@pytest.mark.parametrize(
    "damage", ["packages", "revision", "source", "host", "helpers", "linked-report"]
)
def test_selection_revalidates_materials_instead_of_trusting_success(
    subject, repository, tmp_path, boundary, damage
):
    boundary["root"] = repository
    args = arguments(repository, tmp_path)
    subject.run(args)
    filename = args.output / "manifest.json"
    result = json.loads(filename.read_text())
    if damage == "packages":
        result["packages"]["pip"] = "0"
    elif damage == "revision":
        result["source_revision"] = "b" * 40
    elif damage == "source":
        (repository / "README.md").write_text("moved after bootstrap")
    elif damage == "host":
        result["host"]["sha256"] = "0" * 64
    elif damage == "helpers":
        result["helpers"] = {}
    if damage == "linked-report":
        moved = tmp_path / "moved report"
        filename.rename(moved)
        filename.symlink_to(moved)
    else:
        filename.write_text(json.dumps(result))
    with pytest.raises(ValueError):
        subject.selection(repository, args.output, args.revision)


def test_hidden_worktree_byte_changes_fail_before_any_python(
    subject, repository, tmp_path, boundary
):
    boundary["root"] = repository
    git(repository, "update-index", "--assume-unchanged", "README.md")
    (repository / "README.md").write_text("hidden dirty bytes")
    assert not git(repository, "status", "--porcelain")
    with pytest.raises(ValueError):
        subject.run(arguments(repository, tmp_path))
    assert not boundary["calls"]


def test_real_wheel_sandbox_hides_host_files_and_has_a_distinct_network_namespace(
    subject, tmp_path
):
    from tools.ci import light_environment_sandbox as sandbox

    builder, downloads, wheels = (
        tmp_path / name for name in ("builder", "downloads", "wheels")
    )
    for folder in (builder, downloads, wheels):
        folder.mkdir()
    lock = tmp_path / "requirements.txt"
    lock.write_text("fixture")
    secret = tmp_path / "host-only-private-fixture"
    secret.write_text("must stay outside sandbox")
    marker = downloads / "downloaded"
    marker.write_text("verified fixture")
    code = """import json, pathlib, os
secret, marker, wheel = map(pathlib.Path, __import__('sys').argv[1:])
assert not secret.exists()
assert marker.read_text() == 'verified fixture'
try:
    marker.write_text('changed')
except OSError:
    pass
else:
    raise SystemExit('download was writable')
wheel.write_text('built fixture')
print(json.dumps({'net':os.readlink('/proc/self/ns/net'),'isolated':True}))
"""
    args = sandbox.command(builder, downloads, wheels, lock)
    result = subprocess.run(
        [
            *args,
            "--",
            str(Path(sys.executable).resolve()),
            "-I",
            "-c",
            code,
            str(secret),
            str(marker),
            str(wheels / "built"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["isolated"] is True
    assert data["net"] != os.readlink("/proc/self/ns/net")
    assert (wheels / "built").read_text() == "built fixture"
    assert marker.read_text() == "verified fixture"


@pytest.mark.parametrize(
    "job,failure",
    [
        ("lint", "check"),
        ("lint", "format"),
        ("typecheck", "mypy"),
        ("security", "requirements.txt"),
        ("security", "requirements-ci.txt"),
    ],
)
def test_light_job_consumers_propagate_tool_and_both_audit_failures(
    tmp_path, job, failure
):
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    commands = "\n".join(step.get("run", "") for step in workflow["jobs"][job]["steps"])
    tools = tmp_path / "tools"
    tools.mkdir()
    python = tools / "python"
    python.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, pathlib, sys
args = sys.argv[1:]
if args[:1] != ['-m']:
    raise SystemExit(2)
if args[1] == 'pip_audit':
    if not all(flag in args for flag in ['--strict','--no-deps','--disable-pip','--require-hashes']):
        raise SystemExit(2)
    if '--ignore-vuln' in args:
        raise SystemExit(2)
with pathlib.Path(os.environ['CONSUMED']).open('a') as output:
    output.write(json.dumps(args) + '\\n')
if os.environ['FAIL_TOOL'] in args:
    raise SystemExit(17)
"""
    )
    python.chmod(0o700)
    consumed = tmp_path / "consumed"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", commands],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": str(tools) + os.pathsep + os.environ["PATH"],
            "CONSUMED": str(consumed),
            "FAIL_TOOL": failure,
        },
    )
    assert result.returncode == 17, result.stdout + result.stderr
    seen = [json.loads(row) for row in consumed.read_text().splitlines()]
    assert failure in seen[-1]
    if failure == "requirements-ci.txt":
        assert len(seen) == 3
        assert "requirements.txt" in seen[1]
    if job == "lint" and failure == "check":
        assert len(seen) == 1


def test_selected_complete_lock_set_is_dynamic(subject, repository, tmp_path, boundary):
    lock = repository / "docker/core/requirements-ci.txt"
    lock.write_text(
        "alpha==1 \\\n    --hash=sha256:"
        + "a" * 64
        + "\nbeta==2 \\\n    --hash=sha256:"
        + "b" * 64
        + "\n"
    )
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "selected fixture lock")
    boundary["root"] = repository
    args = arguments(repository, tmp_path)
    subject.run(args)
    report = json.loads((args.output / "manifest.json").read_text())
    assert report["packages"] == {"alpha": "1", "beta": "2"}
    assert subject.selection(repository, args.output, args.revision) == str(
        args.output / "environment"
    )
