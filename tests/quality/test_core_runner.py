"""Isolated core CI must bind real source, resources, execution and cleanup."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import shutil

import pytest

from tools.ci import core_runner as runner
from tools.ci import core_source as source
from tools.ci.python_executor import InvalidExecutor
from tools.ci.core_resources import Docker, Resources, ResourceFailure

IMAGE = "sha256:" + "1" * 64
POSTGRES = "sha256:" + "2" * 64


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    commands = [
        ["init", "-q"],
        ["config", "user.name", "Fixture"],
        ["config", "user.email", "fixture@example.invalid"],
        ["config", "commit.gpgsign", "false"],
        ["config", "core.hooksPath", "/dev/null"],
    ]
    for args in commands:
        subprocess.run(["git", "-C", str(root), *args], check=True)
    (root / "allowed.txt").write_text("committed value\n")
    (root / ".gitignore").write_text("owner.env\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "fixture only"], check=True
    )
    return root


def revision(root):
    return (
        subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"])
        .decode()
        .strip()
    )


def test_archive_contains_only_committed_source_without_git_or_ignored_credentials(
    repository, tmp_path
):
    (repository / "owner.env").write_text("NEVER_ARCHIVE_PRIVATE_VALUE")
    output = tmp_path / "evidence"
    source.reserve_output(repository, output)
    snapshot = source.snapshot(
        repository, output, revision(repository), development=False
    )
    assert (output / "source/allowed.txt").read_text() == "committed value\n"
    assert not (output / "source/owner.env").exists()
    assert not (output / "source/.git").exists()
    assert snapshot["revision"] == revision(repository)
    assert set(snapshot["files"]) == {".gitignore", "allowed.txt"}
    source.unchanged(repository, output, snapshot)


@pytest.mark.parametrize("development", [False, True])
def test_dirty_checkout_never_substitutes_uncommitted_application_source(
    repository, tmp_path, development
):
    (repository / "allowed.txt").write_text("uncommitted changed value")
    output = tmp_path / "evidence"
    source.reserve_output(repository, output)
    if development:
        result = source.snapshot(
            repository, output, revision(repository), development=True
        )
        assert result["worktree"]["dirty"] is True
        assert (output / "source/allowed.txt").read_text() == "committed value\n"
    else:
        with pytest.raises(source.SourceFailure, match="clean"):
            source.snapshot(repository, output, revision(repository), development=False)


@pytest.mark.parametrize("target", ["working", "snapshot"])
def test_changed_source_cannot_pass_at_end(repository, tmp_path, target):
    output = tmp_path / "evidence"
    source.reserve_output(repository, output)
    captured = source.snapshot(
        repository, output, revision(repository), development=False
    )
    base = repository if target == "working" else output / "source"
    (base / "allowed.txt").write_text("changed after execution")
    with pytest.raises(source.SourceFailure, match="changed"):
        source.unchanged(repository, output, captured)


def test_revision_mismatch_and_reused_output_preserve_original_artifacts(
    repository, tmp_path
):
    output = tmp_path / "evidence"
    source.reserve_output(repository, output)
    marker = output / "prior.log"
    marker.write_text("keep original")
    with pytest.raises(source.SourceFailure):
        source.reserve_output(repository, output)
    assert marker.read_text() == "keep original"
    with pytest.raises(source.SourceFailure, match="revision"):
        source.snapshot(repository, output, "a" * 40, development=False)


@pytest.mark.parametrize("value", ["inside", "relative", "symlink"])
def test_output_requires_new_absolute_private_nonlinked_external_directory(
    repository, tmp_path, value
):
    output = repository / "evidence" if value == "inside" else Path("relative")
    if value == "symlink":
        link = tmp_path / "link"
        link.symlink_to(tmp_path / "real")
        output = link / "evidence"
    with pytest.raises(source.SourceFailure):
        source.reserve_output(repository, output)


def test_committed_symlink_is_rejected_even_if_git_archive_could_read_it(
    repository, tmp_path
):
    (repository / "escape").symlink_to("/etc/passwd")
    subprocess.run(["git", "-C", str(repository), "add", "escape"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "test escaping link"],
        check=True,
    )
    output = tmp_path / "evidence"
    source.reserve_output(repository, output)
    with pytest.raises(source.SourceFailure, match="link|regular"):
        source.snapshot(repository, output, revision(repository), development=False)
    assert not (output / "source/escape").exists()


def test_container_environment_cannot_inherit_owner_secrets_or_live_provider_routes(
    monkeypatch,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "do-not-copy")
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner:private@production/db")
    monkeypatch.setenv("DOCKER_HOST", "tcp://other-host:2375")
    env = runner.safe_environment("disposable-only")
    assert env["KLUKAI_TEST_ALLOW_LIVE_BACKENDS"] == "0"
    assert env["KLUKAI_CLAUDE_PRIMARY"] == "false"
    assert env["KLUKAI_IMAGE_PROVIDER"] == "disabled"
    assert env["COVERAGE_FILE"] == "/evidence/.coverage"
    assert env["DATABASE_URL"].startswith("postgresql://disabled:disabled@127.0.0.1:1/")
    assert (
        env["KLUKAI_MIGRATION_TEST_DATABASE_URL"]
        == "postgresql://core_test:disposable-only@postgres:5432/core_test"
    )
    assert all(
        "do-not-copy" not in value
        and "production" not in value
        and "other-host" not in value
        for value in env.values()
    )


def test_core_profile_is_nonroot_readonly_bounded_and_has_only_source_evidence_mounts(
    tmp_path,
):
    flags = runner.core_flags(
        tmp_path / "source", tmp_path / "evidence", "private-network"
    )
    for required in [
        "--user=1000:1000",
        "--read-only",
        "--cpus=2",
        "--memory=2g",
        "--memory-swap=2g",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=256",
    ]:
        assert required in flags
    assert "--network=private-network" in flags
    assert sum(arg.startswith("--mount=") for arg in flags) == 2
    assert any("target=/source,readonly" in arg for arg in flags)
    assert any("target=/evidence" in arg and "readonly" not in arg for arg in flags)
    assert any(arg.startswith("--tmpfs=/tmp:") and "noexec" in arg for arg in flags)
    assert not any(
        "/var/run/docker.sock" in arg or "/dev/" in arg or "network=host" in arg
        for arg in flags
    )


class FakeDocker:
    """A stateful Docker daemon fault double; expected effects are asserted below."""

    def __init__(self, output, fault=""):
        self.output, self.fault = output, fault
        self.calls = []
        self.containers = {}
        self.network = None
        self.cid = "3" * 64

    def __call__(self, argv, **kwargs):
        args = argv[1:]
        self.calls.append(args)
        text, error, code = "", "", 0
        if args[:2] == ["network", "create"]:
            label = next(value for value in args if value.startswith("--label="))[8:]
            key, token = label.split("=", 1)
            self.network = {
                "Id": "4" * 64,
                "Name": args[-1],
                "Labels": {key: token},
                "Internal": True,
                "Containers": {},
            }
            text = "4" * 64
        elif args[:2] == ["network", "inspect"]:
            if self.network is None:
                code, error = (
                    1,
                    "Error response from daemon: network " + args[-1] + " not found",
                )
            else:
                text = json.dumps([self.network])
        elif args[:2] == ["network", "rm"]:
            self.network = None
        elif args[:1] == ["create"]:
            resource = json.loads((self.output / "resources.json").read_text())[
                "containers"
            ][-1]
            assert resource["name"] and resource.get("id") is None, (
                "ownership handle must precede create"
            )
            label = next(value for value in args if value.startswith("--label="))[8:]
            key, token = label.split("=", 1)
            info = {
                "Id": self.cid,
                "Name": "/" + resource["name"],
                "Image": IMAGE,
                "Config": {"Image": IMAGE, "Labels": {key: token}},
                "State": {"Running": False, "ExitCode": 0},
            }
            self.containers[resource["name"]] = info
            text = self.cid
            if self.fault == "create-response-lost":
                raise subprocess.TimeoutExpired(argv, 1)
        elif args[:1] == ["inspect"]:
            name = args[-1]
            info = next(
                (
                    value
                    for key, value in self.containers.items()
                    if name in (key, value["Id"])
                ),
                None,
            )
            if info is None:
                code, error = 1, "Error: No such object: " + name
            else:
                if self.fault == "foreign-label":
                    info["Config"]["Labels"] = {"foreign": "owner"}
                text = json.dumps([info])
        elif args[:1] == ["start"]:
            info = next(iter(self.containers.values()))
            info["State"]["Running"] = self.fault in {"timeout", "cancelled"}
            info["State"]["ExitCode"] = 9 if self.fault == "failed-container" else 0
            if self.fault == "cancelled":
                raise KeyboardInterrupt("cancelled while container runs")
            if self.fault == "timeout":
                raise subprocess.TimeoutExpired(argv, 1, output="partial raw output")
        elif args[:1] in (["stop"], ["kill"]):
            if args[0] == "stop" and self.fault == "stop-timeout":
                raise subprocess.TimeoutExpired(argv, 15)
            next(iter(self.containers.values()))["State"]["Running"] = False
        elif args[:1] == ["wait"]:
            text = "9" if self.fault in {"failed-container", "wait-mismatch"} else "0"
        elif args[:1] == ["rm"]:
            self.containers.clear()
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(argv, code, text, error)


@pytest.mark.parametrize("fault", ["timeout", "cancelled", "create-response-lost", ""])
def test_resources_retain_handles_and_stop_wait_remove_only_owned_objects(
    monkeypatch, tmp_path, fault
):
    fake = FakeDocker(tmp_path, fault)
    monkeypatch.setattr(subprocess, "run", fake)
    resources = Resources(Docker(tmp_path), tmp_path)
    resources.create_network()
    try:
        cid = resources.create_container("core", IMAGE, [])
        if fault in {"timeout", "cancelled"}:
            with pytest.raises(
                KeyboardInterrupt if fault == "cancelled" else subprocess.TimeoutExpired
            ):
                resources.attach(cid, timeout=0.01)
    except subprocess.TimeoutExpired:
        assert fault == "create-response-lost"
    finally:
        resources.close()
    assert not fake.containers and fake.network is None
    assert any(call[0] == "wait" for call in fake.calls)
    assert all(
        call[-1] == fake.cid for call in fake.calls if call[0] in {"stop", "wait", "rm"}
    )
    assert json.loads((tmp_path / "resources.json").read_text())["cleaned"] is True
    if fault in {"timeout", "cancelled"}:
        assert (
            next(i for i, c in enumerate(fake.calls) if c[0] == "stop")
            < next(i for i, c in enumerate(fake.calls) if c[0] == "wait")
            < next(i for i, c in enumerate(fake.calls) if c[0] == "rm")
        )


def test_cleanup_refuses_foreign_label_even_with_a_matching_name(monkeypatch, tmp_path):
    fake = FakeDocker(tmp_path, "foreign-label")
    monkeypatch.setattr(subprocess, "run", fake)
    resources = Resources(Docker(tmp_path), tmp_path)
    resources.create_network()
    resources.create_container("core", IMAGE, [])
    with pytest.raises(ResourceFailure, match="ownership"):
        resources.close()
    assert not any(call[0] in {"stop", "kill", "wait", "rm"} for call in fake.calls)


def test_attached_cli_zero_cannot_hide_nonzero_actual_container_exit(
    monkeypatch, tmp_path
):
    fake = FakeDocker(tmp_path, "failed-container")
    monkeypatch.setattr(subprocess, "run", fake)
    resources = Resources(Docker(tmp_path), tmp_path)
    resources.create_network()
    cid = resources.create_container("core", IMAGE, [])
    try:
        result = resources.attach(cid, timeout=1)
        assert result == {"attach_exit_code": 0, "container_exit_code": 9}
    finally:
        resources.close()


def test_writable_evidence_cannot_alias_the_readonly_source_tree(tmp_path):
    with pytest.raises(ResourceFailure, match="separate"):
        runner.core_flags(tmp_path / "source", tmp_path, "private-network")


def test_timeout_during_stop_still_kills_and_awaits_owned_container(
    monkeypatch, tmp_path
):
    fake = FakeDocker(tmp_path, "stop-timeout")
    monkeypatch.setattr(subprocess, "run", fake)
    resources = Resources(Docker(tmp_path), tmp_path)
    resources.create_network()
    resources.create_container("core", IMAGE, [])
    next(iter(fake.containers.values()))["State"]["Running"] = True
    resources.close()
    effects = [call[0] for call in fake.calls]
    assert (
        effects.index("stop")
        < effects.index("kill")
        < effects.index("wait")
        < effects.index("rm")
    )
    assert not fake.containers and fake.network is None


def test_wait_exit_mismatch_cannot_be_reported_as_cleaned(monkeypatch, tmp_path):
    fake = FakeDocker(tmp_path, "wait-mismatch")
    monkeypatch.setattr(subprocess, "run", fake)
    resources = Resources(Docker(tmp_path), tmp_path)
    resources.create_network()
    resources.create_container("core", IMAGE, [])
    with pytest.raises(ResourceFailure, match="exit"):
        resources.close()
    assert json.loads((tmp_path / "resources.json").read_text())["cleaned"] is False


def valid_core_info(tmp_path):
    return {
        "Config": {"User": "1000:1000", "Env": ["CI=1"]},
        "NetworkSettings": {
            "Networks": {"isolated": {"Gateway": "", "IPv6Gateway": ""}}
        },
        "HostConfig": {
            "ReadonlyRootfs": True,
            "NanoCpus": 2_000_000_000,
            "Memory": 2 * 1024**3,
            "MemorySwap": 2 * 1024**3,
            "PidsLimit": 256,
            "Privileged": False,
            "NetworkMode": "isolated",
            "Dns": ["127.0.0.1"],
            "IpcMode": "private",
            "PidMode": "",
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=512m,mode=1777"},
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": str(tmp_path / "source"),
                "Destination": "/source",
                "RW": False,
            },
            {
                "Type": "bind",
                "Source": str(tmp_path / "results"),
                "Destination": "/evidence",
                "RW": True,
            },
        ],
    }


@pytest.mark.parametrize(
    "fault",
    [
        "secret",
        "database",
        "cpu",
        "memory",
        "gpu",
        "hostpid",
        "socket",
        "source-write",
        "executable-tmp",
    ],
)
def test_actual_container_profile_rejects_extra_routes_mounts_and_capabilities(
    tmp_path, fault
):
    info = valid_core_info(tmp_path)
    runner.validate_core_profile(
        info, tmp_path / "source", tmp_path / "results", "isolated", {"CI": "1"}
    )
    if fault in {"secret", "database"}:
        info["Config"]["Env"].append(
            "ANTHROPIC_API_KEY=secret"
            if fault == "secret"
            else "DATABASE_URL=postgresql://live/production"
        )
    elif fault == "cpu":
        info["HostConfig"]["NanoCpus"] = 4_000_000_000
    elif fault == "memory":
        info["HostConfig"]["MemorySwap"] = -1
    elif fault == "gpu":
        info["HostConfig"]["DeviceRequests"] = [{"Capabilities": [["gpu"]]}]
    elif fault == "hostpid":
        info["HostConfig"]["PidMode"] = "host"
    elif fault == "socket":
        info["Mounts"].append(
            {
                "Type": "bind",
                "Source": "/var/run/docker.sock",
                "Destination": "/socket",
                "RW": True,
            }
        )
    elif fault == "source-write":
        info["Mounts"][0]["RW"] = True
    else:
        info["HostConfig"]["Tmpfs"]["/tmp"] = "rw,nosuid,nodev"
    with pytest.raises(ResourceFailure):
        runner.validate_core_profile(
            info, tmp_path / "source", tmp_path / "results", "isolated", {"CI": "1"}
        )


def write_validation(tmp_path):
    (tmp_path / "executor-probe.json").write_text(
        json.dumps({"python_version": "3.14.8", "packages": {"pytest": "9.0.2"}})
    )
    result = {
        "passed": True,
        "pytest_exit_code": 0,
        "python_executable": "/opt/ci/bin/python",
        "coverage": {"statements_percent": 96, "branches_percent": 95.2},
        "junit": {"passed": 10, "skipped": 2, "tests": 12},
    }
    for name in [
        "pytest.log",
        "coverage.json",
        "coverage-recomputed.json",
        ".coverage",
        "junit.xml",
    ]:
        (tmp_path / name).write_text("real output placeholder")
    (tmp_path / "validation.json").write_text(json.dumps(result))
    return result


@pytest.mark.parametrize(
    "fault",
    ["failed", "zero", "skips", "branches", "missing", "linked", "python", "packages"],
)
def test_failed_or_incomplete_in_image_results_never_pass_host_gate(tmp_path, fault):
    result = write_validation(tmp_path)
    if fault == "failed":
        result["pytest_exit_code"] = 1
    elif fault == "zero":
        result["junit"] = {"passed": 0, "skipped": 12, "tests": 12}
    elif fault == "skips":
        result["junit"] = {"passed": 1, "skipped": 76, "tests": 77}
    elif fault == "branches":
        result["coverage"]["branches_percent"] = 94.99
    elif fault in {"missing", "linked"}:
        (tmp_path / "junit.xml").unlink()
        if fault == "linked":
            (tmp_path / "junit.xml").symlink_to(tmp_path / "pytest.log")
    else:
        probe = {
            "python_version": "3.12.9" if fault == "python" else "3.14.8",
            "packages": {"pytest": "unlocked"},
        }
        (tmp_path / "executor-probe.json").write_text(json.dumps(probe))
    (tmp_path / "validation.json").write_text(json.dumps(result))
    with pytest.raises(
        ValueError if fault in {"python", "packages"} else ResourceFailure
    ):
        runner.load_validation(tmp_path, {"pytest": "9.0.2"})


def commit_producer_helpers(repository):
    directory = repository / "tools/ci"
    directory.mkdir(parents=True)
    for filename in (
        "core_runner.py",
        "core_source.py",
        "core_resources.py",
        "core_profile.py",
        "python_executor.py",
        "cancellation.py",
    ):
        shutil.copyfile(Path(runner.__file__).with_name(filename), directory / filename)
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "producer fixture"], check=True
    )


def test_missing_docker_retains_failed_manifest_and_source_binding(
    repository, tmp_path, monkeypatch
):
    class MissingDocker(Docker):
        def run(self, *args, **kwargs):
            raise FileNotFoundError("docker is absent")

    lock = repository / "docker/core/requirements-ci.txt"
    lock.parent.mkdir(parents=True)
    lock.write_text("pytest==9.0.2\n")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "runner fixture"], check=True
    )
    commit_producer_helpers(repository)
    monkeypatch.setattr(runner, "Docker", MissingDocker)
    output = tmp_path / "run"
    report = runner.execute(
        runner.RunConfig(repository, output, revision(repository), IMAGE, POSTGRES)
    )
    assert report["passed"] is report["certifying"] is False
    assert report["error_type"] == "FileNotFoundError"
    assert report["source"]["revision"] == revision(repository)
    assert json.loads((output / "manifest.json").read_text())["passed"] is False


@pytest.mark.parametrize(
    "fault",
    [
        "revision",
        "lock",
        "source-change",
        "cancel",
        "linked-evidence",
        "success",
        "development",
        "signal-start",
        "signal-cleanup",
        "signal-write",
        "cleanup-failure",
    ],
)
def test_execution_failure_preserves_noncertifying_manifest_and_cleanup(
    repository, tmp_path, monkeypatch, fault
):
    lock = repository / "docker/core/requirements-ci.txt"
    lock.parent.mkdir(parents=True)
    lock.write_text("pytest==9.0.2\n")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "runner fixture"], check=True
    )
    commit_producer_helpers(repository)
    rev = revision(repository)

    class StubDocker(Docker):
        def run(self, *args, **kwargs):
            if args[0] in {"version", "start"}:
                return subprocess.CompletedProcess(args, 0, "{}", "")
            raise AssertionError(args)

        def container(self, identity):
            return valid_core_info(tmp_path)

    class StubResources:
        network = "isolated"

        def __init__(self, docker, output):
            self.output = output

        def create_network(self):
            pass

        def create_container(self, role, image, flags, **kwargs):
            if role == "core":
                assert "target=/evidence" in next(
                    x for x in flags if x.endswith("target=/evidence")
                )
                assert f"source={self.output / 'results'}," in next(
                    x for x in flags if x.endswith("target=/evidence")
                )
                if fault == "source-change":
                    (repository / "allowed.txt").write_text("changed during CI")
                elif fault == "linked-evidence":
                    (self.output / "results/escape").symlink_to("/etc/passwd")
                elif fault == "cancel":
                    raise KeyboardInterrupt("deterministic cancellation")
            return "5" * 64

        def attach(self, cid, **kwargs):
            if fault == "signal-start":
                import os
                import signal

                os.kill(os.getpid(), signal.SIGTERM)
            return {"attach_exit_code": 0, "container_exit_code": 0}

        def close(self):
            if fault in {"signal-cleanup", "signal-start"}:
                import os
                import signal

                os.kill(os.getpid(), signal.SIGINT)
            (self.output / "cleanup-awaited").write_text(
                "owned resources stopped and awaited"
            )
            if fault == "cleanup-failure":
                raise ResourceFailure("genuine owned cleanup failure")

    expected_lock = source.sha256(lock)

    def image_info(docker, identity):
        if identity == POSTGRES:
            return {"Id": POSTGRES, "Config": {"Env": ["PG_MAJOR=17"]}}
        return {
            "Id": IMAGE if fault != "identity" else POSTGRES,
            "Architecture": "amd64",
            "Os": "linux",
            "Config": {
                "User": "1000:1000",
                "Entrypoint": ["/opt/ci/bin/python"],
                "Env": [],
                "Labels": {
                    "org.opencontainers.image.revision": "a" * 40
                    if fault == "revision"
                    else "unverified"
                    if fault == "development"
                    else rev,
                    "cc.appnest.klukai.ci-lock-sha256": "f" * 64
                    if fault == "lock"
                    else expected_lock,
                },
            },
        }

    monkeypatch.setattr(runner, "Docker", StubDocker)
    monkeypatch.setattr(runner, "Resources", StubResources)
    monkeypatch.setattr(runner, "image_info", image_info)
    monkeypatch.setattr(runner, "ready_postgres", lambda *args: "170009")
    monkeypatch.setattr(runner, "validate_core_profile", lambda *args: None)
    monkeypatch.setattr(runner, "validate_postgres_profile", lambda *args: None)
    monkeypatch.setattr(runner, "load_validation", lambda *args: {"passed": True})
    output = tmp_path / "run"
    original_write = Path.write_text
    delivered = []

    def write(path, *args, **kwargs):
        if (
            fault == "signal-write"
            and path.name == "manifest.json.tmp"
            and len(delivered) < 2
        ):
            import os
            import signal

            signum = signal.SIGTERM if not delivered else signal.SIGINT
            delivered.append(signum)
            os.kill(os.getpid(), signum)
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    report = runner.execute(
        runner.RunConfig(
            repository, output, rev, IMAGE, POSTGRES, development=fault == "development"
        )
    )
    assert report["passed"] is (fault in {"success", "development"})
    assert report["certifying"] is (fault == "success")
    assert (
        json.loads((output / "manifest.json").read_text())["certifying"]
        is report["certifying"]
    )
    assert report["source"]["revision"] == rev
    if fault in {"source-change", "cancel", "linked-evidence"}:
        assert (output / "cleanup-awaited").is_file()
        expected = {
            "source-change": "source_error",
            "cancel": "error_type",
            "linked-evidence": "evidence_error",
        }[fault]
        assert expected in report
        if fault == "cancel":
            assert report["error_type"] == "KeyboardInterrupt"
    elif fault == "cleanup-failure":
        assert "genuine owned cleanup failure" in report["cleanup_error"]
        assert report["cancelled"] is False
    elif fault.startswith("signal-"):
        assert report["cancelled"] is True
        assert report["error_type"] == "Cancelled"
        assert (output / "cleanup-awaited").is_file()
        assert report["source_unchanged"] is True
    elif fault not in {"success", "development"}:
        assert report["error_type"] == InvalidExecutor.__name__


@pytest.mark.parametrize(
    "identity", ["latest", "postgres:17", IMAGE, "postgres@sha256:" + "a" * 64]
)
def test_uncached_mutable_or_mismatched_image_reference_is_rejected(tmp_path, identity):
    class ImageDocker(Docker):
        def run(self, *args, **kwargs):
            return subprocess.CompletedProcess(
                args, 0, json.dumps([{"Id": POSTGRES, "RepoDigests": []}]), ""
            )

    with pytest.raises(ResourceFailure):
        runner.image_info(ImageDocker(tmp_path), identity)


@pytest.mark.parametrize(
    "fault", ["", "port", "memory", "volume", "tmp-exec", "env", "absent"]
)
def test_postgres_actual_profile_must_keep_private_disposable_bounds(tmp_path, fault):
    from tools.ci.core_profile import validate_postgres_profile

    info = valid_core_info(tmp_path)
    info["Config"]["User"] = "postgres"
    info["HostConfig"].update(
        {
            "NanoCpus": 500_000_000,
            "Memory": 384 * 1024**2,
            "MemorySwap": 384 * 1024**2,
            "PidsLimit": 128,
            "Tmpfs": {
                key: "rw,noexec,nosuid,nodev"
                for key in ["/tmp", "/var/run/postgresql", "/var/lib/postgresql/data"]
            },
        }
    )
    info["Mounts"] = []
    if fault == "port":
        info["HostConfig"]["PortBindings"] = {"5432/tcp": [{"HostPort": "5432"}]}
    elif fault == "memory":
        info["HostConfig"]["Memory"] = 0
    elif fault == "volume":
        info["Mounts"] = [{"Type": "volume"}]
    elif fault == "tmp-exec":
        info["HostConfig"]["Tmpfs"]["/tmp"] = "rw"
    elif fault == "env":
        info["Config"]["Env"].append("PGHOST=production")
    elif fault == "absent":
        info = None
    if fault:
        with pytest.raises(ResourceFailure):
            validate_postgres_profile(info, "isolated", {"CI": "1"})
    else:
        validate_postgres_profile(info, "isolated", {"CI": "1"})


def test_runner_helpers_must_belong_to_the_committed_snapshot(repository, tmp_path):
    output = tmp_path / "run"
    report = runner.execute(
        runner.RunConfig(repository, output, revision(repository), IMAGE, POSTGRES)
    )
    assert report["passed"] is report["certifying"] is False
    assert report["error_type"] == "SourceFailure"
    assert "producer" in report["error"].lower()


def test_changed_producer_bytes_cannot_certify_other_clean_source(
    repository, tmp_path, monkeypatch
):
    commit_producer_helpers(repository)
    output = tmp_path / "run"
    source.reserve_output(repository, output)
    captured = source.snapshot(
        repository, output, revision(repository), development=False
    )
    fake = tmp_path / "producer"
    shutil.copytree(repository / "tools/ci", fake)
    (fake / "core_profile.py").write_text("changed provider payload")
    monkeypatch.setattr(runner, "__file__", str(fake / "core_runner.py"))
    with pytest.raises(source.SourceFailure, match="Producer"):
        runner.producer_hashes(captured)


@pytest.mark.parametrize("name", ["executor-probe.json", "validation.json"])
def test_linked_result_metadata_is_rejected_before_host_reads_its_target(
    tmp_path, name
):
    write_validation(tmp_path)
    external = tmp_path / "private-target"
    external.write_text("not JSON; host-private marker")
    (tmp_path / name).unlink()
    (tmp_path / name).symlink_to(external)
    with pytest.raises(ResourceFailure, match="linked"):
        runner.load_validation(tmp_path, {"pytest": "9.0.2"})


@pytest.mark.parametrize("first", [15, 2])
def test_repeated_real_signals_do_not_interrupt_owned_resource_reaping(
    monkeypatch, tmp_path, first
):
    import os
    import signal

    fake = FakeDocker(tmp_path)
    phase = {"cleanup": False, "initial": False, "repeat": False}

    def handler(signum, frame):
        raise ResourceFailure(f"cancelled by actual signal {signum}")

    def docker(argv, **kwargs):
        args = argv[1:]
        if args[0] == "start":
            value = fake(argv, **kwargs)
            next(iter(fake.containers.values()))["State"]["Running"] = True
            phase["initial"] = True
            os.kill(os.getpid(), first)
            return value
        if phase["cleanup"] and args[0] == "inspect" and not phase["repeat"]:
            phase["repeat"] = True
            os.kill(
                os.getpid(),
                signal.SIGINT if first == signal.SIGTERM else signal.SIGTERM,
            )
        if args[0] == "rm" and fake.network:
            fake.network["Containers"].clear()
        return fake(argv, **kwargs)

    previous = {
        sig: signal.signal(sig, handler) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    cleanup_error = None
    try:
        monkeypatch.setattr(subprocess, "run", docker)
        resources = Resources(Docker(tmp_path), tmp_path)
        resources.create_network()
        cid = resources.create_container("core", IMAGE, [])
        fake.network["Containers"] = {cid: {"Name": "owned"}}
        with pytest.raises(ResourceFailure, match="actual signal"):
            resources.attach(cid, timeout=1)
        phase["cleanup"] = True
        try:
            resources.close()
        except BaseException as error:
            cleanup_error = error
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)
    assert phase["initial"] and phase["repeat"]
    assert cleanup_error is None
    assert not fake.containers and fake.network is None
    assert json.loads((tmp_path / "resources.json").read_text())["cleaned"] is True


def test_cancelled_normal_phase_cannot_start_more_container_work(monkeypatch, tmp_path):
    import os
    import signal
    from tools.ci.cancellation import Cancelled, cancellation_scope

    calls = []

    def docker(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", docker)
    with cancellation_scope():
        with pytest.raises(Cancelled):
            os.kill(os.getpid(), signal.SIGTERM)
        with pytest.raises(Cancelled):
            Docker(tmp_path).run("start", "3" * 64)
    assert calls == []


@pytest.mark.parametrize("fault", ["absent", "permission", "id", "image", "token"])
def test_cleanup_requires_exact_ownership_or_proven_absence(
    monkeypatch, tmp_path, fault
):
    fake = FakeDocker(tmp_path)

    def docker(argv, **kwargs):
        if argv[1] == "inspect" and fault == "permission":
            return subprocess.CompletedProcess(argv, 1, "", "permission denied")
        return fake(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", docker)
    resources = Resources(Docker(tmp_path), tmp_path)
    resources.create_network()
    cid = resources.create_container("core", IMAGE, [])
    if fault == "absent":
        fake.containers.clear()
        resources.close()
    else:
        info = next(iter(fake.containers.values()))
        fake.network["Containers"] = {cid: {"Name": "owned-handle"}}
        if fault == "id":
            info["Id"] = "6" * 64
        elif fault == "image":
            info["Config"]["Image"] = POSTGRES
        elif fault == "token":
            info["Config"]["Labels"] = {"cc.appnest.klukai.core-run": "wrong-token"}
        with pytest.raises(ResourceFailure):
            resources.close()
    assert not any(call[0] in {"stop", "kill", "wait", "rm"} for call in fake.calls)
    state = json.loads((tmp_path / "resources.json").read_text())
    assert state["cleaned"] is (fault == "absent")
