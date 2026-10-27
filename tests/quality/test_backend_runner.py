"""Managed backend gate rejects substituted context, results and capabilities."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

try:
    from tools.ci import backend_profile as profile
except ImportError:
    profile = None
from tools.ci.core_resources import ResourceFailure

ROOT = Path(__file__).resolve().parents[2]


def context_loader():
    path = ROOT / "docker/core/tests/runtime_backend_context.py"
    assert path.is_file(), "Managed fixture context validation is not implemented"
    spec = importlib.util.spec_from_file_location("managed_context", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_context


def context_fixture(tmp_path):
    source = tmp_path / "source"
    files = {}
    for name in [
        "docker/core/tests/runtime_backends.py",
        "docker/core/tests/runtime_backend_support.py",
        "docker/core/tests/runtime_backend_context.py",
        "docker/core/requirements-ci.txt",
    ]:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
        files[name] = hashlib.sha256(name.encode()).hexdigest()
    value = {
        "schema_version": 1,
        "token": "a" * 32,
        "revision": "b" * 40,
        "module": "tests/runtime_backends.py",
        "source_sha256": files,
        "password": "fresh_disposable_password_123456789",
        "backends": {
            name: {
                "host": "backend-" + "a" * 32 + "-" + name,
                "port": port,
                "image_id": "sha256:" + digit * 64,
            }
            for name, port, digit in [
                ("postgres", 5432, "1"),
                ("redis", 6379, "2"),
            ]
        },
    }
    path = tmp_path / "context/context.json"
    path.parent.mkdir(mode=0o700)
    path.write_text(json.dumps(value))
    path.chmod(0o400)
    return source, path, value


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "hash",
        "alias",
        "port",
        "extra",
        "password",
        "source",
        "link",
        "module",
        "schema",
        "missing-binding",
        "parent-link",
    ],
)
def test_managed_context_accepts_only_exact_private_source_bound_inputs(
    tmp_path, fault
):
    source, path, value = context_fixture(tmp_path)
    if fault == "alias":
        value["backends"]["postgres"]["host"] = "production.example"
    elif fault == "port":
        value["backends"]["redis"]["port"] = True
    elif fault == "extra":
        value["endpoint"] = "https://production.example"
    elif fault == "password":
        value["password"] = ""
    elif fault == "module":
        value["module"] = "tests/health_runtime_backends.py"
    elif fault == "schema":
        value["schema_version"] = True
    elif fault == "missing-binding":
        value["source_sha256"].pop("docker/core/tests/runtime_backend_support.py")
    path.chmod(0o600)
    path.write_text(json.dumps(value))
    path.chmod(0o400)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if fault == "hash":
        digest = "0" * 64
    elif fault == "source":
        (source / "docker/core/tests/runtime_backends.py").write_text("changed")
    elif fault == "link":
        target = path.with_name("target")
        path.rename(target)
        path.symlink_to(target)
    elif fault == "parent-link":
        alias = tmp_path / "alias"
        alias.symlink_to(path.parent)
        path = alias / path.name
    loader = context_loader()
    if fault == "none":
        result = loader(path, digest, "tests/runtime_backends.py", source)
        assert result["password"] == "fresh_disposable_password_123456789"
        assert result["backends"]["postgres"]["port"] == 5432
    else:
        with pytest.raises(ValueError):
            loader(path, digest, "tests/runtime_backends.py", source)


RUNTIME_CASES = [
    "test_postgres_auth_failure_and_real_protocol_recovery",
    "test_redis_session_failure_closed_and_preserved_owner_recovery",
    "test_postgres_memory_recall_full_text_candidates_and_haiku_pick",
    "test_postgres_memory_recall_without_haiku_keeps_postgres_order",
    "test_postgres_memory_loss_fails_open_and_recall_recovers",
    "test_real_query_cancellation_releases_pool",
]


def junit_file(path, module="tests/runtime_backends.py"):
    names = RUNTIME_CASES
    root = ET.Element("testsuites")
    suite = ET.SubElement(
        root,
        "testsuite",
        tests=str(len(names)),
        failures="0",
        errors="0",
        skipped="0",
    )
    for name in names:
        ET.SubElement(
            suite,
            "testcase",
            classname=module.removesuffix(".py").replace("/", "."),
            name=name,
        )
    ET.ElementTree(root).write(path)


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "empty",
        "skip",
        "duplicate",
        "wrong-module",
        "wrong-name",
        "failure",
        "count",
        "linked",
    ],
)
def test_junit_must_prove_actual_exact_driver_identities_without_skips(tmp_path, fault):
    assert profile is not None, "Exact backend JUnit validation is not implemented"
    path = tmp_path / "junit.xml"
    junit_file(path)
    tree = ET.parse(path)
    suite = tree.getroot()[0]
    if fault == "empty":
        suite.clear()
    elif fault == "skip":
        ET.SubElement(suite[0], "skipped")
    elif fault == "duplicate":
        suite[1].set("name", suite[0].get("name"))
    elif fault == "wrong-module":
        suite[0].set("classname", "tests.test_fake")
    elif fault == "wrong-name":
        suite[0].set("name", "test_fake")
    elif fault == "failure":
        ET.SubElement(suite[0], "failure")
    elif fault == "count":
        suite.set("tests", "400")
    tree.write(path)
    if fault == "linked":
        target = tmp_path / "target.xml"
        path.rename(target)
        path.symlink_to(target)
    if fault == "none":
        assert profile.junit_results(path, "tests/runtime_backends.py")["passed"] == 6
    else:
        with pytest.raises(ResourceFailure):
            profile.junit_results(path, "tests/runtime_backends.py")


def worker_info(tmp_path):
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
                "Source": str(tmp_path / source),
                "Destination": dest,
                "RW": writable,
            }
            for source, dest, writable in [
                ("source", "/source", False),
                ("results", "/evidence", True),
                ("context", "/runtime-context", False),
            ]
        ],
    }


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "context-write",
        "socket",
        "privileged",
        "gpu",
        "home",
        "network",
        "cpus",
        "guard",
        "context-alias",
    ],
)
def test_actual_worker_inspect_enforces_closed_capabilities_and_readonly_context(
    tmp_path, fault
):
    info = worker_info(tmp_path)
    if fault == "context-write":
        info["Mounts"][2]["RW"] = True
    elif fault == "socket":
        info["Mounts"].append(
            {
                "Type": "bind",
                "Source": "/var/run/docker.sock",
                "Destination": "/socket",
                "RW": True,
            }
        )
    elif fault == "privileged":
        info["HostConfig"]["Privileged"] = True
    elif fault == "gpu":
        info["HostConfig"]["DeviceRequests"] = [{"Capabilities": [["gpu"]]}]
    elif fault == "home":
        info["Mounts"][1]["Source"] = "/home/jalsarraf"
    elif fault == "network":
        info["NetworkSettings"]["Networks"]["public"] = {"Gateway": "172.17.0.1"}
    elif fault == "cpus":
        info["HostConfig"]["NanoCpus"] = 0
    elif fault == "guard":
        info["Config"]["Env"].append("OTEL_SDK_DISABLED=false")
    elif fault == "context-alias":
        info["Mounts"][2]["Source"] = str(tmp_path / "results")
    args = (
        info,
        tmp_path / "source",
        tmp_path / "results",
        tmp_path / "context",
        "isolated",
        {"CI": "1"},
    )
    if fault == "none":
        profile.validate_worker(*args)
    else:
        with pytest.raises(ResourceFailure):
            profile.validate_worker(*args)


def test_worker_context_cannot_alias_writable_evidence(tmp_path):
    with pytest.raises(ResourceFailure):
        profile.worker_flags(
            tmp_path / "source",
            tmp_path / "results",
            tmp_path / "results/context",
            "isolated",
        )


def test_worker_environment_keeps_global_sdk_guards():
    env = profile.worker_environment("tests/runtime_backends.py", "a" * 64)
    assert env["OTEL_SDK_DISABLED"] == "true"
    assert env["DATABASE_URL"] == "postgresql://disabled:disabled@127.0.0.1:1/disabled"
    assert env["ANTHROPIC_API_KEY"] == env["OPENAI_API_KEY"] == ""
    assert env["KLUKAI_RUNTIME_CONTEXT"] == "/runtime-context/context.json"


def test_backend_service_profile_refuses_host_mounts_or_extra_aliases(tmp_path):
    from tools.ci import backend_services as services

    info = worker_info(tmp_path)
    info["Mounts"] = []
    info["Name"] = "/owned"
    info["HostConfig"].update(
        {
            "NanoCpus": 300_000_000,
            "Memory": 96 * 1024**2,
            "MemorySwap": 96 * 1024**2,
            "PidsLimit": 128,
            "Tmpfs": {
                "/tmp": "rw,noexec,nosuid,nodev,size=32m,mode=1777",
                "/data": "rw,noexec,nosuid,nodev,size=64m,mode=1777",
            },
        }
    )
    info["NetworkSettings"]["Networks"]["isolated"]["Aliases"] = [
        "owned",
        "backend-" + "a" * 32 + "-redis",
    ]
    services.validate_service(info, "redis", "isolated", {"CI": "1"}, "a" * 32)
    info["Mounts"] = [{"Type": "bind", "Source": "/home/jalsarraf", "RW": True}]
    with pytest.raises(ResourceFailure):
        services.validate_service(info, "redis", "isolated", {"CI": "1"}, "a" * 32)


def test_backend_runner_producer_is_bound_to_committed_snapshot(tmp_path):
    from tools.ci import backend_runner as runner

    with pytest.raises(ValueError, match="[Pp]roducer"):
        runner.producer_hashes({"files": {}})


def backend_runner_fixture(tmp_path):
    import shutil
    import subprocess
    from tools.ci import backend_runner as runner

    repo = tmp_path / "repository"
    repo.mkdir()
    for args in [
        ["init", "-q"],
        ["config", "user.name", "Fixture"],
        ["config", "user.email", "fixture@example.invalid"],
        ["config", "commit.gpgsign", "false"],
        ["config", "core.hooksPath", "/dev/null"],
    ]:
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    for name in [
        "backend_runner.py",
        "backend_services.py",
        "backend_profile.py",
        "core_runner.py",
        "core_source.py",
        "core_resources.py",
        "core_profile.py",
        "python_executor.py",
        "cancellation.py",
    ]:
        path = repo / "tools/ci" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(runner.__file__).with_name(name), path)
    lock = repo / "docker/core/requirements-ci.txt"
    lock.parent.mkdir(parents=True)
    lock.write_text("pytest==9.0.2\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    revision = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    return runner, repo, revision


def test_missing_host_docker_leaves_noncertifying_source_bound_manifest(
    tmp_path, monkeypatch
):
    from tools.ci.core_resources import Docker

    runner, repo, revision = backend_runner_fixture(tmp_path)

    class MissingDocker(Docker):
        def run(self, *args, **kwargs):
            raise FileNotFoundError("docker is absent")

    monkeypatch.setattr(runner, "Docker", MissingDocker)
    output = tmp_path / "run"
    report = runner.execute(
        runner.RunConfig(
            repo, output, revision, *["sha256:" + digit * 64 for digit in "123"]
        )
    )
    assert report["passed"] is report["certifying"] is False
    assert report["error_type"] == "FileNotFoundError"
    assert report["source_unchanged"] is True
    assert (
        json.loads((output / "manifest.json").read_text())["source_revision"]
        == revision
    )


@pytest.mark.parametrize(
    "fault",
    [
        "success",
        "development",
        "source",
        "producer",
        "cancel",
        "context",
        "cleanup",
        "signal-cleanup",
        "signal-write",
    ],
)
def test_orchestration_closes_owned_sets_and_never_certifies_interrupted_inputs(
    tmp_path, monkeypatch, fault
):
    import os
    import signal
    import subprocess
    from tools.ci.core_resources import Docker

    runner, repo, revision = backend_runner_fixture(tmp_path)
    images = {
        name: "sha256:" + digit * 64
        for name, digit in [
            ("core", "1"),
            ("postgres", "2"),
            ("redis", "3"),
        ]
    }
    instances = []

    class HostDocker(Docker):
        def run(self, *args, **kwargs):
            return subprocess.CompletedProcess(args, 0, "{}", "")

        def container(self, cid):
            return {"Id": cid, "Image": images["core"]}

    class OwnedSet:
        def __init__(self, docker, output):
            self.docker, self.output = docker, output
            self.token = str(len(instances) + 1) * 32
            self.network = "isolated-" + self.token
            self.state = {"cleaned": False, "containers": []}
            instances.append(self)

        def create_network(self):
            pass

        def create_container(self, role, image, flags, **kwargs):
            if fault == "source":
                (repo / "tools/ci/backend_profile.py").write_text("changed")
            elif fault == "context":
                (self.output / "results/escape").symlink_to("/etc/passwd")
            return "5" * 64

        def attach(self, cid, **kwargs):
            if fault == "cancel":
                os.kill(os.getpid(), signal.SIGTERM)
            return {"attach_exit_code": 0, "container_exit_code": 0}

        def close(self):
            if fault in {"cancel", "signal-cleanup"}:
                os.kill(os.getpid(), signal.SIGINT)
                os.kill(os.getpid(), signal.SIGTERM)
            (self.output / "reaped").write_text("cleanup awaited")
            self.state["cleaned"] = True
            if fault == "cleanup":
                raise ResourceFailure("owned cleanup failed")

    def image_info(docker, identity):
        return {
            "Id": identity,
            "Config": {
                "Env": ["PG_MAJOR=17"] if identity == images["postgres"] else []
            },
        }

    monkeypatch.setattr(runner, "Docker", HostDocker)
    monkeypatch.setattr(runner, "Resources", OwnedSet)
    monkeypatch.setattr(runner.core_runner, "image_info", image_info)
    monkeypatch.setattr(runner, "validate_image", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner.backend_services,
        "start_services",
        lambda *args: {
            name: {"host": name, "port": port, "image_id": images[name]}
            for name, port in [("postgres", 5432), ("redis", 6379)]
        },
    )
    monkeypatch.setattr(runner.backend_profile, "validate_worker", lambda *args: None)
    monkeypatch.setattr(
        runner.backend_profile, "load_validation", lambda *args: {"passed": True}
    )
    monkeypatch.setattr(
        runner.backend_profile, "validate_runtime_evidence", lambda *args: {}
    )
    if fault == "producer":
        original = runner.producer_hashes
        called = []

        def changed(captured):
            called.append(True)
            if len(called) > 1:
                raise ValueError("Producer was changed")
            return original(captured)

        monkeypatch.setattr(runner, "producer_hashes", changed)
    original_write = Path.write_text
    signals = []

    def write(path, *args, **kwargs):
        if fault == "signal-write" and path.name == "manifest.json.tmp" and not signals:
            signals.append(True)
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGINT)
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    output = tmp_path / "run"
    report = runner.execute(
        runner.RunConfig(
            repo, output, revision, *images.values(), development=fault == "development"
        )
    )
    assert report["passed"] is (fault in {"success", "development"})
    assert report["certifying"] is (fault == "success")
    assert all(
        instance.state["cleaned"] and (instance.output / "reaped").is_file()
        for instance in instances
    )
    if fault in {"success", "development"}:
        assert len(instances) == 3
        assert len({instance.token for instance in instances}) == 3
        assert {result["module"] for result in report["modules"]} == {
            "tests/runtime_backends.py",
            "tests/health_runtime_backends.py",
            "tests/lifecycle_runtime_backends.py",
        }
    if fault in {"cancel", "signal-cleanup", "signal-write"}:
        assert report["cancelled"] is True
        assert len(report["signals"]) >= 2
    assert (
        json.loads((output / "manifest.json").read_text())["certifying"]
        is report["certifying"]
    )


@pytest.mark.parametrize(
    "fault", ["none", "missing", "binding", "cleanup", "unexpected", "linked"]
)
def test_complete_raw_driver_evidence_and_source_bindings_are_required(tmp_path, fault):
    source = {"docker/core/app/db.py": "a" * 64}
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    expected = {
        "containers.json": {"postgres": {"host": "owned", "port": 5432}},
        **{
            name + ".json": {
                "source_sha256": {"app/db.py": "a" * 64},
                "states": [{"phase": "observed"}],
            }
            for name in [
                "postgres",
                "redis",
                "cancellation",
                "memory-recall",
                "memory-recall-fallback",
                "memory-recall-recovery",
            ]
        },
        **{
            name + "-cleanup.json": {
                "pool_closed": True,
                "http_closed": True,
                "proxy_tasks": 0,
                "proxy_streams": 0,
            }
            for name in RUNTIME_CASES
        },
    }
    for name, value in expected.items():
        (runtime / name).write_text(json.dumps(value))
    if fault == "missing":
        (runtime / "cancellation.json").unlink()
    elif fault == "binding":
        (runtime / "postgres.json").write_text(
            json.dumps({"source_sha256": {"app/db.py": "b" * 64}, "states": [{}]})
        )
    elif fault == "cleanup":
        path = runtime / "test_real_query_cancellation_releases_pool-cleanup.json"
        value = json.loads(path.read_text())
        value["proxy_tasks"] = 1
        path.write_text(json.dumps(value))
    elif fault == "unexpected":
        (runtime / "unknown.json").write_text("{}")
    elif fault == "linked":
        (runtime / "cancellation.json").unlink()
        (runtime / "cancellation.json").symlink_to(runtime / "postgres.json")
    if fault == "none":
        result = profile.validate_runtime_evidence(
            tmp_path, "tests/runtime_backends.py", source, expected["containers.json"]
        )
        assert len(result) == 13
    else:
        with pytest.raises(ResourceFailure):
            profile.validate_runtime_evidence(
                tmp_path,
                "tests/runtime_backends.py",
                source,
                expected["containers.json"],
            )


@pytest.mark.parametrize("capability", ["CapAdd", "ExtraHosts", "VolumesFrom", "Links"])
def test_worker_rejects_hidden_host_capabilities_even_with_correct_primary_profile(
    tmp_path, capability
):
    info = worker_info(tmp_path)
    info["HostConfig"][capability] = ["unexpected"]
    with pytest.raises(ResourceFailure):
        profile.validate_worker(
            info,
            tmp_path / "source",
            tmp_path / "results",
            tmp_path / "context",
            "isolated",
            {"CI": "1"},
        )


@pytest.mark.parametrize(
    "fault",
    ["python", "packages", "skip", "nonzero", "empty", "linked", "context", "module"],
)
def test_worker_summary_cannot_replace_actual_junit_probe_or_context(tmp_path, fault):
    junit_file(tmp_path / "junit.xml")
    (tmp_path / "pytest.log").write_text("4 passed")
    probe = {"python_version": "3.14.8", "packages": {"pytest": "9.0.2"}}
    result = {
        "passed": True,
        "pytest_exit_code": 0,
        "python_executable": "/opt/ci/bin/python",
        "module": "tests/runtime_backends.py",
        "context_sha256": "a" * 64,
        "junit": profile.junit_results(
            tmp_path / "junit.xml", "tests/runtime_backends.py"
        ),
    }
    if fault == "python":
        probe["python_version"] = "3.12.9"
    elif fault == "packages":
        probe["packages"] = {"pytest": "unlocked"}
    elif fault == "skip":
        tree = ET.parse(tmp_path / "junit.xml")
        ET.SubElement(tree.getroot()[0][0], "skipped")
        tree.write(tmp_path / "junit.xml")
    elif fault == "nonzero":
        result["pytest_exit_code"] = 1
    elif fault == "empty":
        (tmp_path / "pytest.log").write_text("")
    elif fault == "linked":
        (tmp_path / "pytest.log").unlink()
        (tmp_path / "pytest.log").symlink_to(tmp_path / "junit.xml")
    elif fault == "context":
        result["context_sha256"] = "b" * 64
    elif fault == "module":
        result["module"] = "tests/lifecycle_runtime_backends.py"
    (tmp_path / "executor-probe.json").write_text(json.dumps(probe))
    (tmp_path / "validation.json").write_text(json.dumps(result))
    with pytest.raises((ResourceFailure, ValueError)):
        profile.load_validation(
            tmp_path, "tests/runtime_backends.py", "a" * 64, {"pytest": "9.0.2"}
        )


def test_backend_set_is_postgres_and_valkey_only_everywhere():
    """Memory lives in PostgreSQL (cloud-only AI): no vector store to provision."""
    from tools.ci import backend_runner as runner
    from tools.ci import backend_services as services

    context = importlib.util.spec_from_file_location(
        "managed_context_ports", ROOT / "docker/core/tests/runtime_backend_context.py"
    )
    module = importlib.util.module_from_spec(context)
    context.loader.exec_module(module)
    assert profile.BACKENDS == ("postgres", "redis")
    assert services.PORTS == module.PORTS == {"postgres": 5432, "redis": 6379}
    assert set(services.LIMITS) == set(profile.BACKENDS)
    assert "qdrant_image" not in runner.RunConfig.__dataclass_fields__
    assert sum(len(cases) for cases in profile.CASES.values()) == 10
    assert profile.evidence_names("tests/runtime_backends.py") >= {
        name + ".json" for name in profile.RECALL_EVIDENCE
    }
    for module_name in profile.CASES:
        assert not any("qdrant" in name for name in profile.evidence_names(module_name))
        assert not any("qdrant" in name for name in profile.CASES[module_name])


@pytest.mark.parametrize(
    "fault",
    [
        "opt-in",
        "evidence",
        "context-path",
        "context-hash",
        "context-missing",
        "manual-missing-docker",
    ],
)
def test_fixture_mode_selection_fails_closed_without_falling_back_to_docker(
    tmp_path, fault
):
    import os
    import subprocess
    import sys

    env = profile.worker_environment("tests/runtime_backends.py", "a" * 64)
    env.update(
        {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": str(ROOT / "docker/core"),
            "PERSONALITY_PATH": str(ROOT / "config/personality.yaml"),
        }
    )
    for name in [
        "KLUKAI_RUNTIME_MODULE",
        "KLUKAI_RUNTIME_CONTEXT",
        "KLUKAI_RUNTIME_CONTEXT_SHA256",
    ]:
        env.pop(name)
    env["KLUKAI_RUNTIME_EVIDENCE"] = str(tmp_path / "evidence")
    if fault == "opt-in":
        env.pop("KLUKAI_RUNTIME_BACKENDS")
    elif fault == "evidence":
        env.pop("KLUKAI_RUNTIME_EVIDENCE")
    elif fault == "context-path":
        env["KLUKAI_RUNTIME_CONTEXT"] = "/untrusted/context.json"
        env["KLUKAI_RUNTIME_CONTEXT_SHA256"] = "a" * 64
    elif fault == "context-hash":
        env["KLUKAI_RUNTIME_CONTEXT_SHA256"] = "a" * 64
    elif fault == "context-missing":
        env["KLUKAI_RUNTIME_MODULE"] = "tests/runtime_backends.py"
    script = """
from types import SimpleNamespace
from tests import runtime_backend_support as support
import pytest
calls = []
def missing(*args, **kwargs):
    calls.append(args)
    raise FileNotFoundError("No Docker capability")
support.docker = missing
request = SimpleNamespace(module=SimpleNamespace(__file__="runtime_backends.py"))
try:
    next(support.backends.__wrapped__(None, request))
except (pytest.fail.Exception, ValueError, FileNotFoundError):
    print("docker-calls=" + str(len(calls)))
else:
    raise AssertionError("Invalid fixture mode was accepted")
"""
    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", script],
        cwd=ROOT / "docker/core",
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "docker-calls=1" if fault == "manual-missing-docker" else "docker-calls=0"
    )
