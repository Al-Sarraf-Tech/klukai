"""A host's Python version cannot certify the production interpreter."""

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def executor():
    path = ROOT / "tools/ci/python_executor.py"
    spec = importlib.util.spec_from_file_location("python_executor", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lock_versions_handles_hash_lines_and_normalizes_names(executor):
    text = """# generated lock
Pytest==9.0.3 \\
    --hash=sha256:abc
typing_extensions==4.15.0 \\
    --hash=sha256:def
psycopg[binary]==3.3.4 \\
    --hash=sha256:ghi
"""
    assert executor.lock_versions(text) == {
        "pytest": "9.0.3",
        "typing-extensions": "4.15.0",
        "psycopg": "3.3.4",
    }


@pytest.mark.parametrize(
    "text",
    [
        "",
        "pytest>=9",
        "https://example.test/code.whl",
        "-r other.txt",
        "pytest==9.0.3\nPytest==9.0.2",
        "--index-url https://example.test",
    ],
)
def test_invalid_or_nonexclusive_locks_fail(executor, text):
    with pytest.raises(executor.InvalidExecutor):
        executor.lock_versions(text)


def test_probe_accepts_actual_matching_interpreter_and_all_locked_packages(executor):
    expected = {"pytest": "9.0.3", "httpx2": "2.13.1"}
    probe = {"python_version": "3.14.8", "packages": expected.copy()}
    executor.validate_probe(probe, expected)


@pytest.mark.parametrize(
    "probe",
    [
        None,
        [],
        {},
        {"python_version": "3.12.3", "packages": {"pytest": "9.0.3"}},
        {"python_version": "3.14.8", "packages": {}},
        {"python_version": "3.14.8", "packages": {"pytest": "9.0.2"}},
        {"python_version": "3.14.8", "packages": {"pytest": "9.0.3", "extra": "1"}},
    ],
)
def test_probe_rejects_ambient_python_missing_old_or_extra_packages(executor, probe):
    with pytest.raises(executor.InvalidExecutor):
        executor.validate_probe(probe, {"pytest": "9.0.3"})


def test_image_metadata_binds_id_source_lock_and_nonroot(executor):
    identity = "sha256:" + "1" * 64
    revision, lock = "a" * 40, "b" * 64
    info = {
        "Id": identity,
        "Architecture": "amd64",
        "Os": "linux",
        "Config": {
            "User": "1000:1000",
            "Entrypoint": ["/opt/ci/bin/python"],
            "Labels": {
                "org.opencontainers.image.revision": revision,
                "cc.appnest.klukai.ci-lock-sha256": lock,
            },
        },
    }
    executor.validate_image(info, identity, revision, lock)
    for mapping, key, bad in [
        (info, "Id", "sha256:" + "2" * 64),
        (info, "Architecture", "arm64"),
        (info, "Os", "windows"),
        (info["Config"], "User", "0"),
        (info["Config"], "Entrypoint", ["python3"]),
        (info["Config"]["Labels"], "org.opencontainers.image.revision", "unverified"),
        (info["Config"]["Labels"], "cc.appnest.klukai.ci-lock-sha256", "c" * 64),
    ]:
        previous = mapping[key]
        mapping[key] = bad
        with pytest.raises(executor.InvalidExecutor):
            executor.validate_image(info, identity, revision, lock)
        mapping[key] = previous


def test_ci_build_uses_same_digest_pinned_interpreter_as_runtime():
    image = ROOT / "docker/core/Dockerfile.ci"
    assert image.is_file(), "CI must not use the heavy host's Python 3.12"
    source = image.read_text()
    runtime = (ROOT / "docker/core/Dockerfile").read_text()
    expected = "python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170"
    assert expected in source and expected in runtime
    assert "--require-hashes" in source
    assert "--network=none" in source
    assert "USER 1000:1000" in source


def test_timed_out_probe_removes_only_its_owned_container(
    executor, monkeypatch, tmp_path
):
    identity, cid = "sha256:" + "1" * 64, "2" * 64
    calls, labels = [], {}
    state = {"exists": False, "running": False, "exit": 0}

    def docker(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "create":
            key, value = next(
                arg[8:] for arg in argv if arg.startswith("--label=")
            ).split("=", 1)
            labels[key] = value
            state["exists"] = True
            return subprocess.CompletedProcess(argv, 0, cid, "")
        if argv[1] == "start":
            state["running"] = True
            raise subprocess.TimeoutExpired(argv, 0.01)
        if argv[1] == "inspect":
            if not state["exists"]:
                return subprocess.CompletedProcess(
                    argv, 1, "", "No such object: " + argv[-1]
                )
            info = {
                "Id": cid,
                "Config": {"Image": identity, "Labels": labels},
                "State": {"Running": state["running"], "ExitCode": state["exit"]},
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps([info]), "")
        if argv[1] == "stop":
            state["running"], state["exit"] = False, 143
        elif argv[1] == "wait":
            return subprocess.CompletedProcess(argv, 0, str(state["exit"]), "")
        elif argv[1] == "rm":
            assert argv == ["docker", "rm", "--volumes", cid]
            state["exists"] = False
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(executor.subprocess, "run", docker)
    with pytest.raises(subprocess.TimeoutExpired):
        executor.probe_container(identity, tmp_path, timeout=0.01)
    actions = [argv[1] for argv in calls]
    assert actions.index("stop") < actions.index("wait") < actions.index("rm")
    assert state["exists"] is state["running"] is False
    assert json.loads((tmp_path / "container.json").read_text())["removed"] is True
    assert (
        json.loads((tmp_path / "probe-manifest.json").read_text())["error_type"]
        == "TimeoutExpired"
    )


def test_probe_cleanup_refuses_a_container_with_different_ownership(
    executor, monkeypatch, tmp_path
):
    identity, cid = "sha256:" + "1" * 64, "2" * 64
    calls = []

    def docker(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "create":
            return subprocess.CompletedProcess(argv, 0, cid + "\n", "")
        if argv[1] == "start":
            raise subprocess.TimeoutExpired(argv, 0.01)
        if argv[1] == "inspect":
            info = {"Id": cid, "Config": {"Image": identity, "Labels": {}}}
            return subprocess.CompletedProcess(argv, 0, json.dumps([info]), "")
        raise AssertionError("Unowned resource must not be removed")

    monkeypatch.setattr(executor.subprocess, "run", docker)
    with pytest.raises(executor.InvalidExecutor, match="ownership"):
        executor.probe_container(identity, tmp_path, timeout=0.01)
    assert all(argv[1] != "rm" for argv in calls)
    report = json.loads((tmp_path / "probe-manifest.json").read_text())
    assert report["passed"] is report["certifying"] is False
    assert "ownership" in report["cleanup_error"]


@pytest.mark.parametrize("first", [15, 2])
@pytest.mark.parametrize("phase", ["start-repeat", "cleanup-first", "evidence-first"])
def test_repeated_real_signals_preserve_probe_failure_evidence_and_reap_owned_container(
    executor, monkeypatch, tmp_path, first, phase
):
    import os
    import signal

    identity, cid = "sha256:" + "1" * 64, "2" * 64
    state = {
        "exists": False,
        "running": False,
        "initial": False,
        "repeat": False,
        "calls": [],
        "labels": {},
        "exit": 0,
    }

    def handler(signum, frame):
        raise executor.InvalidExecutor(f"cancelled by actual signal {signum}")

    def docker(argv, **kwargs):
        action = argv[1]
        state["calls"].append(action)
        if action == "create":
            labels = next((arg[8:] for arg in argv if arg.startswith("--label=")), None)
            if labels is None:
                labels = argv[argv.index("--label") + 1]
            key, token = labels.split("=", 1)
            state["labels"][key] = token
            state["exists"] = True
            return subprocess.CompletedProcess(argv, 0, cid, "")
        if action == "start":
            state["running"] = phase == "start-repeat"
            if phase == "start-repeat":
                state["initial"] = True
                os.kill(os.getpid(), first)
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        if action == "inspect":
            if (
                state.get("cleanup")
                and phase == "cleanup-first"
                and not state["initial"]
            ):
                state["initial"] = True
                os.kill(os.getpid(), first)
            elif state.get("cleanup") and state["initial"] and not state["repeat"]:
                state["repeat"] = True
                os.kill(
                    os.getpid(),
                    signal.SIGINT if first == signal.SIGTERM else signal.SIGTERM,
                )
            if not state["exists"]:
                return subprocess.CompletedProcess(
                    argv, 1, "", "No such object: " + argv[-1]
                )
            info = {
                "Id": cid,
                "Config": {"Image": identity, "Labels": state["labels"]},
                "State": {"Running": state["running"], "ExitCode": state["exit"]},
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps([info]), "")
        if action in {"stop", "kill"}:
            state["running"], state["exit"] = False, 143
        elif action == "wait":
            return subprocess.CompletedProcess(argv, 0, str(state["exit"]), "")
        elif action == "rm":
            state["exists"] = False
        elif action not in {"stop", "kill"}:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    original_close = executor.Resources.close
    original_write = Path.write_text

    def close(resource):
        state["cleanup"] = True
        return original_close(resource)

    def write(path, *args, **kwargs):
        if phase == "evidence-first" and path.name == "probe-manifest.json.tmp":
            if not state["initial"]:
                state["initial"] = True
                os.kill(os.getpid(), first)
            elif not state["repeat"]:
                state["repeat"] = True
                os.kill(
                    os.getpid(),
                    signal.SIGINT if first == signal.SIGTERM else signal.SIGTERM,
                )
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(executor.Resources, "close", close)
    monkeypatch.setattr(Path, "write_text", write)
    previous = {
        sig: signal.signal(sig, handler) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    failure = None
    try:
        monkeypatch.setattr(subprocess, "run", docker)
        try:
            executor.probe_container(identity, tmp_path, timeout=1)
        except BaseException as error:
            failure = error
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)
    assert state["initial"] and state["repeat"]
    assert failure is not None
    assert state["exists"] is state["running"] is False
    assert state["calls"].index("wait") < state["calls"].index("rm")
    if phase == "start-repeat":
        assert state["calls"].index("stop") < state["calls"].index("wait")
    report = json.loads((tmp_path / "probe-manifest.json").read_text())
    assert report["passed"] is report["certifying"] is False
    assert report["cancelled"] is True


@pytest.fixture
def source_repository(executor, tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for args in [
        ["init", "-q"],
        ["config", "user.name", "Fixture"],
        ["config", "user.email", "fixture@example.invalid"],
        ["config", "commit.gpgsign", "false"],
        ["config", "core.hooksPath", "/dev/null"],
    ]:
        subprocess.run(["git", "-C", str(root), *args], check=True)
    for name in executor.INPUTS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name.startswith("tools/ci/"):
            path.write_bytes(Path(executor.__file__).with_name(path.name).read_bytes())
        else:
            path.write_text(
                "pytest==9.0.3\n"
                if name.endswith("requirements-ci.txt")
                else "fixture original\n"
            )
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "fixture only"], check=True
    )
    return root


@pytest.mark.parametrize(
    "change",
    [
        "clean",
        "content",
        "head",
        "linked-input",
        "input-set",
        "helper-python_executor.py",
        "helper-cancellation.py",
        "helper-core_resources.py",
        "helper-core_source.py",
        "runtime-helper",
    ],
)
def test_standalone_probe_closes_git_and_input_identity_before_certifying(
    executor, source_repository, tmp_path, monkeypatch, change
):
    import argparse
    import hashlib

    root = source_repository
    if change.startswith("helper-"):
        (root / "tools/ci" / change.removeprefix("helper-")).write_text(
            "# committed placeholder differs from the executing helper\n"
        )
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-qm", "private mismatched helper"],
            check=True,
        )
    revision = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    identity = "sha256:" + "1" * 64
    before = executor.source_identity(root)
    assert before["status"] == ""
    image = {
        "Id": identity,
        "Architecture": "amd64",
        "Os": "linux",
        "Config": {
            "User": "1000:1000",
            "Entrypoint": ["/opt/ci/bin/python"],
            "Labels": {
                "org.opencontainers.image.revision": revision,
                "cc.appnest.klukai.ci-lock-sha256": hashlib.sha256(
                    (root / "docker/core/requirements-ci.txt").read_bytes()
                ).hexdigest(),
            },
        },
    }
    original_run = subprocess.run

    def docker(argv, **kwargs):
        if argv[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps([image]), "")
        return original_run(argv, **kwargs)

    def probe(*args):
        target = root / "docker/core/package_runtime.py"
        if change == "content":
            target.write_text("changed during probe\n")
        elif change == "head":
            (root / "new-input.txt").write_text("changed commit\n")
            original_run(["git", "-C", str(root), "add", "."], check=True)
            original_run(
                ["git", "-C", str(root), "commit", "-qm", "private fixture advance"],
                check=True,
            )
        elif change == "linked-input":
            target.unlink()
            target.symlink_to(root / "docker/core/Dockerfile.ci")
        elif change == "runtime-helper":
            replacement = tmp_path / "changed-executing-helper.py"
            replacement.write_text("# different executing helper identity\n")
            monkeypatch.setattr(executor.resources_module, "__file__", str(replacement))
        elif change == "input-set":
            original_run(
                [
                    "git",
                    "-C",
                    str(root),
                    "rm",
                    "--cached",
                    "docker/core/package_runtime.py",
                ],
                check=True,
                capture_output=True,
            )
        return subprocess.CompletedProcess(
            ["docker", "start", "--attach", "fixture"],
            0,
            json.dumps({"python_version": "3.14.8", "packages": {"pytest": "9.0.3"}}),
            "",
        )

    monkeypatch.setattr(executor.subprocess, "run", docker)
    monkeypatch.setattr(executor, "probe_container", probe)
    output = tmp_path / "result"
    args = argparse.Namespace(
        root=root, output=output, revision=revision, image=identity, development=False
    )
    if change == "clean":
        executor.run(args)
    else:
        with pytest.raises(executor.InvalidExecutor):
            executor.run(args)
    report = json.loads((output / "manifest.json").read_text())
    assert report["source_before"] == before
    assert report["inputs"] == before["inputs"]
    assert report["source_unchanged"] is (change == "clean")
    assert report["passed"] is report["certifying"] is (change == "clean")
    if change != "clean":
        assert "source_error" in report


@pytest.mark.parametrize(
    "helper",
    ["python_executor.py", "cancellation.py", "core_resources.py", "core_source.py"],
)
@pytest.mark.parametrize("fault", ["no-file", "missing", "linked", "different"])
def test_executing_helper_file_identity_is_verified_not_just_selected_root(
    executor, source_repository, tmp_path, monkeypatch, helper, fault
):
    root = source_repository
    source = executor.source_identity(root)
    observed = executor.executing_helpers(root, source)
    logical = "tools/ci/" + helper
    assert observed[logical]["sha256"] == source["inputs"][logical]
    assert Path(observed[logical]["path"]) != root / logical, (
        "equal bytes from a clean clone are allowed"
    )
    modules = {
        "python_executor.py": executor,
        "cancellation.py": executor.cancellation_module,
        "core_resources.py": executor.resources_module,
        "core_source.py": executor.source_module,
    }
    replacement = tmp_path / "replacement.py"
    if fault == "linked":
        replacement.symlink_to(root / logical)
    elif fault == "different":
        replacement.write_text("# alternate implementation\n")
    monkeypatch.setattr(
        modules[helper], "__file__", None if fault == "no-file" else str(replacement)
    )
    with pytest.raises(executor.InvalidExecutor, match="Executing helper"):
        executor.executing_helpers(root, source)
