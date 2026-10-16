"""Tool-image certification binds actual private Git history and build bytes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
MATERIALS = [
    "Dockerfile.ci",
    "Dockerfile.ci.dockerignore",
    "requirements-build.txt",
    "requirements-ci.txt",
    "package_runtime.py",
]
FIRST = 1_700_000_000


def git(root, *args, env=None):
    return subprocess.check_output(
        ["git", "-C", str(root), *args], env=env, text=True
    ).strip()


def commit(root, timestamp, message="fixture"):
    git(root, "add", ".")
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": f"{timestamp} +0000",
        "GIT_COMMITTER_DATE": f"{timestamp} +0000",
    }
    git(root, "commit", "-qm", message, env=env)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    for args in [
        ["init", "-q"],
        ["config", "user.name", "Fixture"],
        ["config", "user.email", "fixture@example.invalid"],
        ["config", "commit.gpgsign", "false"],
        ["config", "core.hooksPath", "/dev/null"],
    ]:
        git(root, *args)
    for name in MATERIALS:
        path = root / "docker/core" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "docker/core" / name, path)
    (root / "README.md").write_text("initial documentation")
    commit(root, FIRST)
    return root


def capture(repository, output):
    from tools.ci.core_source import reserve_output, snapshot

    reserve_output(repository, output)
    return snapshot(
        repository, output, git(repository, "rev-parse", "HEAD"), development=False
    )


def test_material_epoch_tracks_five_last_changes_and_ignores_docs(repository, tmp_path):
    from tools.ci import tool_materials as materials

    first = git(repository, "rev-parse", "HEAD")
    (repository / "README.md").write_text("documentation-only revision")
    commit(repository, FIRST + 100)
    output = tmp_path / "docs-run"
    value = materials.prepare(repository, output, capture(repository, output))
    assert value["source_date_epoch"] == FIRST
    assert {row["commit"] for row in value["last_changes"].values()} == {first}
    path = repository / "docker/core/package_runtime.py"
    path.write_text(path.read_text() + "\n# material revision\n")
    last = commit(repository, FIRST + 200)
    output = tmp_path / "material-run"
    changed = materials.prepare(repository, output, capture(repository, output))
    assert changed["source_date_epoch"] == FIRST + 200
    assert changed["last_changes"]["docker/core/package_runtime.py"]["commit"] == last
    assert changed["last_changes"]["docker/core/Dockerfile.ci"]["commit"] == first


def test_context_and_deterministic_archive_contain_exact_committed_materials(
    repository, tmp_path
):
    from tools.ci import tool_materials as materials

    first = tmp_path / "first"
    second = tmp_path / "second"
    a = materials.prepare(repository, first, capture(repository, first))
    b = materials.prepare(repository, second, capture(repository, second))
    assert a["archive_sha256"] == b["archive_sha256"]
    assert set(a["context_sha256"]) == set(MATERIALS)
    with tarfile.open(first / "context.tar") as package:
        assert package.getnames() == sorted(MATERIALS)
        for member in package.getmembers():
            assert (member.uid, member.gid, member.mtime, member.mode) == (
                0,
                0,
                FIRST,
                0o644,
            )
            assert (
                package.extractfile(member).read()
                == (repository / "docker/core" / member.name).read_bytes()
            )
    assert all(not path.is_dir() for path in (first / "context").iterdir())


@pytest.mark.parametrize(
    "fault", ["extra", "directory", "linked", "changed", "archive", "epoch", "shallow"]
)
def test_closing_material_checks_reject_context_history_or_epoch_substitution(
    repository, tmp_path, fault
):
    from tools.ci import tool_materials as materials
    from tools.ci.core_source import SourceFailure

    output = tmp_path / "run"
    captured = capture(repository, output)
    value = materials.prepare(repository, output, captured)
    if fault == "extra":
        (output / "context/app.py").write_text("extra")
    elif fault == "directory":
        (output / "context/app").mkdir()
    elif fault == "linked":
        path = output / "context/package_runtime.py"
        path.unlink()
        path.symlink_to(repository / "docker/core/package_runtime.py")
    elif fault == "changed":
        (output / "context/package_runtime.py").write_text("changed")
    elif fault == "archive":
        (output / "context.tar").write_bytes(b"changed")
    elif fault == "epoch":
        value["source_date_epoch"] += 1
    else:
        (repository / ".git/shallow").write_text(captured["revision"] + "\n")
    with pytest.raises((SourceFailure, ValueError)):
        materials.unchanged(repository, output, captured, value)


@pytest.mark.parametrize(
    "fault",
    [
        "shallow",
        "missing",
        "linked",
        "mutable-base",
        "mutable-frontend",
        "missing-hash",
    ],
)
def test_material_preparation_fails_before_build_for_unverifiable_inputs(
    repository, tmp_path, fault
):
    from tools.ci import tool_materials as materials
    from tools.ci.core_source import SourceFailure

    if fault == "shallow":
        (repository / ".git/shallow").write_text(
            git(repository, "rev-parse", "HEAD") + "\n"
        )
    elif fault == "missing":
        (repository / "docker/core/package_runtime.py").unlink()
        commit(repository, FIRST + 1)
    elif fault == "linked":
        path = repository / "docker/core/package_runtime.py"
        path.unlink()
        path.symlink_to("requirements-ci.txt")
        commit(repository, FIRST + 1)
    elif fault in {"mutable-base", "mutable-frontend"}:
        path = repository / "docker/core/Dockerfile.ci"
        text = path.read_text()
        if fault == "mutable-base":
            text = text.replace(
                "ARG PYTHON_IMAGE=python:3.14-slim@sha256:",
                "ARG PYTHON_IMAGE=python:3.14-slim #",
            )
        else:
            text = text.replace(
                "# syntax=docker/dockerfile:1.7@sha256:",
                "# syntax=docker/dockerfile:1.7 #",
            )
        path.write_text(text)
        commit(repository, FIRST + 1)
    elif fault == "missing-hash":
        path = repository / "docker/core/requirements-ci.txt"
        path.write_text("pytest==9.0.2\n")
        commit(repository, FIRST + 1)
    output = tmp_path / "run"
    with pytest.raises((SourceFailure, ValueError)):
        materials.prepare(repository, output, capture(repository, output))


def test_directory_build_context_uses_the_same_verified_epoch_as_archive(
    repository, tmp_path
):
    from tools.ci import tool_materials as materials
    from tools.ci.core_source import SourceFailure

    output = tmp_path / "run"
    captured = capture(repository, output)
    value = materials.prepare(repository, output, captured)
    assert {
        (path.stat().st_mtime_ns, path.stat().st_mode & 0o777)
        for path in (output / "context").iterdir()
    } == {(FIRST * 1_000_000_000, 0o644)}
    os.utime(output / "context/package_runtime.py", (FIRST + 500, FIRST + 500))
    with pytest.raises(SourceFailure):
        materials.unchanged(repository, output, captured, value)


def producer_repository(repository):
    for name in [
        "tool_image.py",
        "tool_materials.py",
        "core_source.py",
        "core_resources.py",
        "python_executor.py",
        "cancellation.py",
    ]:
        path = repository / "tools/ci" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "tools/ci" / name, path)
    return commit(repository, FIRST + 500, "producer helpers")


@pytest.mark.parametrize(
    "fault",
    [
        "success",
        "build",
        "missing-iid",
        "bad-iid",
        "linked-iid",
        "wrong-image",
        "wrong-revision",
        "probe",
        "native",
        "packages",
        "source",
        "context",
        "archive",
        "helper",
        "probe-cleanup",
        "timeout",
        "signal-build",
        "signal-probe",
        "signal-write",
    ],
)
def test_builder_certifies_only_one_completed_exact_source_build_and_native_probe(
    repository, tmp_path, monkeypatch, fault
):
    from tools.ci import tool_image as builder
    from tools.ci.core_resources import Docker
    from tools.ci.core_source import sha256
    from tools.ci import cancellation
    import signal

    revision = producer_repository(repository)
    output = tmp_path / "run"
    image = "sha256:" + "a" * 64
    expected_packages = builder.python_executor.lock_versions(
        (repository / "docker/core/requirements-ci.txt").read_text()
    )
    commands = []

    class BuildDocker(Docker):
        def run(self, *args, **kwargs):
            commands.append(args)
            if args[0] == "build":
                assert kwargs["timeout"] == 600
                assert "--pull=false" in args and "--progress=plain" in args
                assert "--build-arg=VCS_REF=" + revision in args
                assert "--build-arg=SOURCE_DATE_EPOCH=1700000000" in args
                assert (
                    "--build-arg=CI_LOCK_SHA256="
                    + sha256(repository / "docker/core/requirements-ci.txt")
                    in args
                )
                assert {p.name for p in (output / "context").iterdir()} == set(
                    MATERIALS
                )
                if fault == "timeout":
                    raise subprocess.TimeoutExpired(
                        args,
                        600,
                        output="partial build stdout",
                        stderr="partial build stderr",
                    )
                if fault == "signal-build":
                    os.kill(os.getpid(), signal.SIGTERM)
                iid = output / "image.iid"
                if fault == "bad-iid":
                    iid.write_text("ci-tool:latest\n")
                elif fault == "linked-iid":
                    target = output / "target"
                    target.write_text(image + "\n")
                    iid.symlink_to(target)
                elif fault != "missing-iid":
                    iid.write_text(image + "\n")
                if fault == "source":
                    (repository / "README.md").write_text(
                        "source changed while building"
                    )
                elif fault == "context":
                    (output / "context/extra").write_text("additional material")
                elif fault == "archive":
                    (output / "context.tar").write_bytes(b"changed")
                return subprocess.CompletedProcess(
                    ["docker", *args],
                    9 if fault == "build" else 0,
                    "build stdout",
                    "build stderr",
                )
            assert args[:2] == ("image", "inspect") and args[-1] == image
            config = {
                "User": "1000:1000",
                "Entrypoint": ["/opt/ci/bin/python"],
                "Labels": {
                    "org.opencontainers.image.revision": "b" * 40
                    if fault == "wrong-revision"
                    else revision,
                    "cc.appnest.klukai.ci-lock-sha256": sha256(
                        repository / "docker/core/requirements-ci.txt"
                    ),
                },
            }
            return subprocess.CompletedProcess(
                ["docker", *args],
                0,
                json.dumps(
                    [
                        {
                            "Id": "sha256:" + "b" * 64
                            if fault == "wrong-image"
                            else image,
                            "Architecture": "amd64",
                            "Os": "linux",
                            "Config": config,
                        }
                    ]
                ),
                "",
            )

    def probe(identity, evidence, *, timeout):
        assert identity == image
        with cancellation.cancellation_scope() as latch:
            try:
                if fault == "signal-probe":
                    os.kill(os.getpid(), signal.SIGTERM)
                if fault in {"probe", "probe-cleanup"}:
                    raise ValueError("primary native probe failure")
            finally:
                with latch.deferred():
                    if fault == "signal-probe":
                        os.kill(os.getpid(), signal.SIGINT)
                    (evidence / "probe-cleanup-awaited").write_text(
                        "owned probe stopped and awaited"
                    )
                    (evidence / "probe-manifest.json").write_text(
                        json.dumps(
                            {
                                "passed": fault
                                not in {"probe", "probe-cleanup", "signal-probe"},
                                "error_type": "ValueError",
                                "error": "primary native probe failure",
                                "cleanup_error": "probe cleanup failed"
                                if fault == "probe-cleanup"
                                else None,
                            }
                        )
                    )
        return subprocess.CompletedProcess(
            ["docker", "start", "--attach", "owned"],
            0,
            json.dumps(
                {
                    "python_version": "3.12.9" if fault == "native" else "3.14.8",
                    "packages": {"pytest": "9.0.2"}
                    if fault == "packages"
                    else expected_packages,
                }
            ),
            "probe stderr",
        )

    def external_build(command, environment, destination, timeout, report, latch):
        docker = BuildDocker(destination)
        try:
            result = docker.run(*command[1:], timeout=timeout)
        except (Exception, KeyboardInterrupt) as error:
            builder.save_streams(destination, "build", error)
            raise
        builder.save_streams(destination, "build", result)
        return result

    monkeypatch.setattr(builder, "run_build", external_build)
    monkeypatch.setattr(builder, "Docker", BuildDocker)
    monkeypatch.setattr(builder.python_executor, "probe_container", probe)
    if fault == "helper":
        original = builder.executing_helpers
        calls = []

        def changed(captured):
            calls.append(True)
            if len(calls) > 1:
                raise ValueError("Executing helper changed")
            return original(captured)

        monkeypatch.setattr(builder, "executing_helpers", changed)
    original_write = Path.write_text
    delivered = []

    def write(path, *args, **kwargs):
        if (
            fault == "signal-write"
            and path.name == "manifest.json.tmp"
            and not delivered
        ):
            delivered.append(True)
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGINT)
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    report = builder.execute(
        builder.RunConfig(repository, output, revision, timeout=600)
    )
    assert report["passed"] is report["certifying"] is (fault == "success")
    assert len([command for command in commands if command[0] == "build"]) == 1
    assert report["build"]["completion_known"] is (
        fault not in {"timeout", "signal-build"}
    )
    assert (output / "build.stdout").exists() and (output / "build.stderr").exists()
    if fault == "build":
        assert report["build"]["exit_code"] == 9
        assert report["error_type"] == "ResourceFailure"
    if fault == "timeout":
        assert "partial build stdout" in (output / "build.stdout").read_text()
    if fault in {"signal-probe", "probe", "probe-cleanup"}:
        assert (output / "probe/probe-cleanup-awaited").is_file()
    if fault.startswith("signal-"):
        assert report["cancelled"] is True
    assert (
        json.loads((output / "manifest.json").read_text())["certifying"]
        is report["certifying"]
    )


@pytest.mark.parametrize(
    "fault",
    [
        "dirty",
        "wrong-head",
        "short-revision",
        "missing-helper",
        "changed-helper",
        "shallow",
        "timeout-bound",
    ],
)
def test_preflight_source_and_history_failure_never_invokes_docker(
    repository, tmp_path, monkeypatch, fault
):
    from tools.ci import tool_image as builder

    revision = (
        producer_repository(repository)
        if fault != "missing-helper"
        else git(repository, "rev-parse", "HEAD")
    )
    if fault == "dirty":
        (repository / "README.md").write_text("uncommitted")
    elif fault == "wrong-head":
        revision = "a" * 40
    elif fault == "short-revision":
        revision = revision[:10]
    elif fault == "changed-helper":
        (repository / "tools/ci/tool_materials.py").write_text("different producer")
        revision = commit(repository, FIRST + 501)
    elif fault == "shallow":
        (repository / ".git/shallow").write_text(revision + "\n")

    class ForbiddenDocker:
        def __init__(self, output):
            raise AssertionError("Preflight unexpectedly invoked Docker")

    monkeypatch.setattr(builder, "Docker", ForbiddenDocker)
    output = tmp_path / "run"
    report = builder.execute(
        builder.RunConfig(
            repository,
            output,
            revision,
            timeout=3601 if fault == "timeout-bound" else 600,
        )
    )
    assert report["passed"] is report["certifying"] is False
    assert "build" not in report
    assert report["error_type"] == "SourceFailure"
    assert json.loads((output / "manifest.json").read_text())["certifying"] is False


def test_failed_native_probe_retains_actual_stdout_and_primary_failure(
    tmp_path, monkeypatch
):
    from tools.ci import cancellation, tool_image as builder

    expected = {"pytest": "9.0.2"}
    observed = json.dumps({"python_version": "3.12.9", "packages": expected})

    def probe(image, output, *, timeout):
        (output / "probe-manifest.json").write_text('{"passed": true}')
        return subprocess.CompletedProcess(["probe"], 0, observed, "native stderr")

    monkeypatch.setattr(builder.python_executor, "probe_container", probe)
    report = {}
    with cancellation.cancellation_scope() as latch:
        with pytest.raises(ValueError, match="interpreter"):
            builder.probe_image("sha256:" + "a" * 64, tmp_path, expected, report, latch)
    assert (tmp_path / "probe/probe.stdout").read_text() == observed
    assert (tmp_path / "probe/probe.stderr").read_text() == "native stderr"


def test_existing_raw_output_link_is_rejected_without_overwriting_target(tmp_path):
    from tools.ci import tool_image as builder

    target = tmp_path / "target"
    target.write_text("private unchanged marker")
    (tmp_path / "build.stdout").symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        builder.save_streams(
            tmp_path,
            "build",
            subprocess.CompletedProcess([], 0, "unsafe overwrite", "stderr"),
        )
    assert target.read_text() == "private unchanged marker"


def test_raw_write_failure_preserves_the_original_probe_error(tmp_path, monkeypatch):
    from tools.ci import cancellation, tool_image as builder

    def broken_streams(*args):
        raise OSError("raw evidence unavailable")

    def failed_probe(*args, **kwargs):
        raise ValueError("primary native import failed")

    monkeypatch.setattr(builder, "save_streams", broken_streams)
    monkeypatch.setattr(builder.python_executor, "probe_container", failed_probe)
    report = {}
    with cancellation.cancellation_scope() as latch:
        with pytest.raises(ValueError, match="primary native import"):
            builder.probe_image(
                "sha256:" + "a" * 64, tmp_path, {"pytest": "9.0.2"}, report, latch
            )
    assert report["probe_stream_error"] == "raw evidence unavailable"


def test_build_client_streams_partial_output_before_real_term_and_is_reaped(tmp_path):
    from tools.ci import cancellation, tool_image as builder
    import signal
    import sys
    import threading
    import time

    assert callable(getattr(builder, "run_build", None)), (
        "Streaming build client is not implemented"
    )
    stop = threading.Event()
    ready = tmp_path / "ready"
    script = (
        "import pathlib,time; print('partial build evidence', flush=True); pathlib.Path("
        + repr(str(ready))
        + ").touch(); time.sleep(30)"
    )
    report = {"build": {"completion_known": False}}

    def terminate():
        deadline = time.monotonic() + 5
        while not ready.exists() and not stop.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        if ready.exists() and not stop.is_set():
            os.kill(os.getpid(), signal.SIGTERM)

    sender = threading.Thread(target=terminate)
    sender.start()
    try:
        with cancellation.cancellation_scope() as latch:
            with pytest.raises(cancellation.Cancelled):
                builder.run_build(
                    [sys.executable, "-c", script],
                    {"PATH": os.environ["PATH"]},
                    tmp_path,
                    10,
                    report,
                    latch,
                )
    finally:
        stop.set()
        sender.join(timeout=6)
    assert report["build"]["completion_known"] is False
    assert report["build"]["client_reaped"] is True
    assert type(report["build"]["client_exit_code"]) is int
    assert (tmp_path / "build.stdout").read_text() == "partial build evidence\n"
    with pytest.raises(ProcessLookupError):
        os.kill(report["build"]["client_pid"], 0)


def test_term_during_client_creation_cannot_escape_owned_reaping(tmp_path, monkeypatch):
    from tools.ci import cancellation, tool_image as builder
    import signal
    import sys

    original = subprocess.Popen
    spawned = []

    def create(*args, **kwargs):
        process = original(*args, **kwargs)
        spawned.append(process)
        os.kill(os.getpid(), signal.SIGTERM)
        return process

    monkeypatch.setattr(builder.subprocess, "Popen", create)
    report = {"build": {"completion_known": False}}
    try:
        with cancellation.cancellation_scope() as latch:
            with pytest.raises(cancellation.Cancelled):
                builder.run_build(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    {"PATH": os.environ["PATH"]},
                    tmp_path,
                    10,
                    report,
                    latch,
                )
        assert report["build"].get("client_reaped") is True
        assert all(process.poll() is not None for process in spawned)
    finally:
        for process in spawned:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


@pytest.mark.parametrize(
    "extra",
    [
        "from python:3.14.8-slim AS final",
        "FrOm python:3.14.8-slim AS final",
        "FROM\tpython:3.14.8-slim AS final",
        "\tfrom\tpython:3.14.8-slim AS final",
        "from \\\n    python:3.14.8-slim AS final",
        "FrOm\t\\\n    python:3.14.8-slim AS final",
        "FR\\\nOM python:3.14.8-slim AS final",
        "FROM \\ \n    python:3.14.8-slim AS final",
        "FROM \\\n# continuation comment\npython:3.14.8-slim AS final",
        "FROM \\\n\npython:3.14.8-slim AS final",
        "FROM python:3.14.8-slim AS final\\",
        "FROM python:3.14.8-slim AS final\u000bRUN true",
    ],
)
def test_recipe_lexing_cannot_hide_an_extra_mutable_stage(extra):
    from tools.ci import tool_materials as materials
    from tools.ci.core_source import SourceFailure

    recipe = (ROOT / "docker/core/Dockerfile.ci").read_text()
    with pytest.raises(SourceFailure):
        materials.validate_recipe(recipe + "\n" + extra + "\n")


@pytest.mark.parametrize(
    "directive",
    [
        "# escape=`",
        "# EsCaPe=\\",
        "# escape = `",
        "# syntax=docker/dockerfile:latest",
        "# check=skip=all",
    ],
)
def test_recipe_rejects_unadmitted_parser_directives_and_escape_modes(directive):
    from tools.ci import tool_materials as materials
    from tools.ci.core_source import SourceFailure

    recipe = (ROOT / "docker/core/Dockerfile.ci").read_text()
    first, rest = recipe.split("\n", 1)
    with pytest.raises(SourceFailure):
        materials.validate_recipe(first + "\n" + directive + "\n" + rest)


@pytest.mark.parametrize(
    "variant", ["current", "crlf", "case-tabs", "continued-from", "shell-payload"]
)
def test_recipe_lexer_preserves_two_pinned_stages_and_logical_shell_payloads(variant):
    from tools.ci import tool_materials as materials

    recipe = (ROOT / "docker/core/Dockerfile.ci").read_text()
    if variant == "crlf":
        recipe = recipe.replace("\n", "\r\n")
    elif variant == "case-tabs":
        recipe = recipe.replace("ARG PYTHON_IMAGE=", "aRg\tPYTHON_IMAGE=").replace(
            "FROM ${PYTHON_IMAGE}", "fRoM\t${PYTHON_IMAGE}"
        )
    elif variant == "continued-from":
        recipe = recipe.replace(
            "FROM ${PYTHON_IMAGE} AS", "FROM \\\n    ${PYTHON_IMAGE} AS"
        )
    elif variant == "shell-payload":
        recipe += "\nRUN echo \\\n    from python:3.14.8-slim AS shell_words\n"
    assert materials.validate_recipe(recipe) == {
        "frontend": "docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e",
        "python_image": "python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170",
    }


@pytest.mark.parametrize("keyword", ["from ", "FrOm ", "FROM\t"])
def test_committed_mutable_final_stage_cannot_prepare_a_certifiable_context(
    repository, tmp_path, keyword
):
    from tools.ci import tool_materials as materials
    from tools.ci.core_source import SourceFailure

    path = repository / "docker/core/Dockerfile.ci"
    path.write_text(
        path.read_text()
        + "\n"
        + keyword
        + 'python:3.14.8-slim AS final\nARG VCS_REF\nARG CI_LOCK_SHA256\nCOPY --from=ci /opt/ci /opt/ci\nUSER 1000:1000\nENTRYPOINT ["/opt/ci/bin/python"]\n'
    )
    commit(repository, FIRST + 700, "mutable final stage counterexample")
    output = tmp_path / "run"
    with pytest.raises(SourceFailure):
        materials.prepare(repository, output, capture(repository, output))
