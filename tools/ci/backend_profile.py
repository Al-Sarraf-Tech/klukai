"""Closed managed-backend worker profile and exact real-driver result checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from .core_profile import core_flags, safe_environment, validate_core_profile
from .core_resources import ResourceFailure
from .python_executor import validate_probe

# Disposable services each driver receives. Memory lives in PostgreSQL only
# (cloud-only AI, 2026-10-26): recall is full-text candidates plus a Haiku
# pick, so there is no vector store to provision or fault.
BACKENDS = ("postgres", "redis")
# Raw evidence the runtime driver's real-PostgreSQL memory recall cases write.
RECALL_EVIDENCE = ("memory-recall", "memory-recall-fallback", "memory-recall-recovery")

CASES = {
    "tests/runtime_backends.py": {
        "test_postgres_auth_failure_and_real_protocol_recovery",
        "test_redis_session_failure_closed_and_preserved_owner_recovery",
        "test_postgres_memory_recall_full_text_candidates_and_haiku_pick",
        "test_postgres_memory_recall_without_haiku_keeps_postgres_order",
        "test_postgres_memory_loss_fails_open_and_recall_recovers",
        "test_real_query_cancellation_releases_pool",
    },
    "tests/health_runtime_backends.py": {
        "test_reused_real_readiness_clients_survive_fault_and_recovery[postgres]",
        "test_reused_real_readiness_clients_survive_fault_and_recovery[redis]",
        "test_real_readiness_cancellation_releases_stream_and_reuses_client",
    },
    "tests/lifecycle_runtime_backends.py": {
        "test_shutdown_retains_accepted_journal_and_next_lifespan_only_recovers",
    },
}

# The worker has no Docker CLI/socket, owner HOME, host devices or credentials.
DRIVER = r"""
import contextlib, io, json, os, pathlib, subprocess, sys, traceback
sys.path[:0] = ['/source', '/source/docker/core']
out = pathlib.Path('/evidence')
pathlib.Path('/tmp/klukai-ci-home').mkdir(mode=0o700, exist_ok=True)
result = {'passed': False, 'python_executable': sys.executable}
try:
    from tools.ci.python_executor import PROBE, lock_versions, validate_probe
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        exec(PROBE, {})
    probe = json.loads(captured.getvalue())
    (out / 'executor-probe.json').write_text(json.dumps(probe, indent=2) + '\n')
    expected = lock_versions(pathlib.Path('/source/docker/core/requirements-ci.txt').read_text())
    validate_probe(probe, expected)
    module = os.environ['KLUKAI_RUNTIME_MODULE']
    from tools.ci.backend_profile import CASES, junit_results
    if module not in CASES:
        raise ValueError('Unknown real-backend driver')
    argv = [sys.executable, '-m', 'pytest', module, '-q', '-W', 'error',
            '-o', 'cache_dir=/tmp/pytest-cache', '--junitxml=/evidence/junit.xml']
    result['pytest_command'] = argv
    with (out / 'pytest.log').open('wb') as stream:
        completed = subprocess.run(argv, cwd='/source/docker/core', stdout=stream,
                                   stderr=subprocess.STDOUT, check=False)
    result['pytest_exit_code'] = completed.returncode
    if completed.returncode != 0:
        raise ValueError('Real-backend pytest failed')
    result['junit'] = junit_results(out / 'junit.xml', module)
    result['module'] = module
    result['context_sha256'] = os.environ['KLUKAI_RUNTIME_CONTEXT_SHA256']
    result['passed'] = True
except BaseException as error:
    result['error_type'], result['error'] = type(error).__name__, str(error)
    traceback.print_exc()
finally:
    (out / 'validation.json').write_text(json.dumps(result, indent=2) + '\n')
sys.exit(0 if result['passed'] else 1)
"""


def worker_environment(module: str, digest: str) -> dict[str, str]:
    env = safe_environment("disabled")
    env["KLUKAI_MIGRATION_TEST_DATABASE_URL"] = ""
    env.update(
        {
            "KLUKAI_RUNTIME_BACKENDS": "1",
            "KLUKAI_RUNTIME_EVIDENCE": "/evidence/runtime",
            "KLUKAI_RUNTIME_CONTEXT": "/runtime-context/context.json",
            "KLUKAI_RUNTIME_CONTEXT_SHA256": digest,
            "KLUKAI_RUNTIME_MODULE": module,
        }
    )
    return env


def worker_flags(source: Path, output: Path, context: Path, network: str) -> list[str]:
    if any(
        a.is_relative_to(b) or b.is_relative_to(a)
        for a, b in [(context, source), (context, output)]
    ):
        raise ResourceFailure("Readonly context must be separate from source/evidence")
    return core_flags(source, output, network) + [
        f"--mount=type=bind,source={context},target=/runtime-context,readonly"
    ]


def validate_closed_host(info: dict[str, Any]) -> None:
    host = info["HostConfig"]
    if any(
        host.get(key)
        for key in [
            "CapAdd",
            "ExtraHosts",
            "VolumesFrom",
            "Links",
            "Binds",
            "DeviceCgroupRules",
            "GroupAdd",
        ]
    ):
        raise ResourceFailure("Unexpected hidden host capability")
    if host.get("SecurityOpt") != ["no-new-privileges:true"]:
        raise ResourceFailure("Unexpected security capability")


def validate_worker(
    info: dict[str, Any],
    source: Path,
    output: Path,
    context: Path,
    network: str,
    env: dict[str, str],
) -> None:
    validate_closed_host(info)
    mounts = info.get("Mounts", [])
    contexts = [m for m in mounts if m.get("Destination") == "/runtime-context"]
    if (
        len(contexts) != 1
        or contexts[0].get("Type") != "bind"
        or contexts[0].get("Source") != str(context)
        or contexts[0].get("RW") is not False
    ):
        raise ResourceFailure("Unexpected managed context mount")
    reduced = {**info, "Mounts": [m for m in mounts if m not in contexts]}
    validate_core_profile(reduced, source, output, network, env)


def required_file(path: Path) -> None:
    if (
        not path.is_file()
        or any(p.is_symlink() for p in (path, *path.parents))
        or not path.stat().st_size
    ):
        raise ResourceFailure("Missing/linked/empty backend execution output")


def junit_results(path: Path, module: str) -> dict[str, Any]:
    required_file(path)
    if module not in CASES:
        raise ResourceFailure("Unknown real driver")
    root = ET.parse(path).getroot()
    suites = list(root.iter("testsuite"))
    cases = list(root.iter("testcase"))
    expected = CASES[module]
    classname = module.removesuffix(".py").replace("/", ".")
    identities = [(case.get("classname"), case.get("name")) for case in cases]
    if (
        len(suites) != 1
        or len(cases) != len(expected)
        or set(identities) != {(classname, name) for name in expected}
        or any(list(case) for case in cases)
    ):
        raise ResourceFailure("Missing/failed/skipped/duplicate real driver identities")
    suite = suites[0]
    if any(
        suite.get(key) != value
        for key, value in {
            "tests": str(len(expected)),
            "failures": "0",
            "errors": "0",
            "skipped": "0",
        }.items()
    ):
        raise ResourceFailure("Inconsistent real JUnit counts")
    return {
        "tests": len(cases),
        "passed": len(cases),
        "skipped": 0,
        "identities": sorted(name for _, name in identities),
    }


def load_validation(
    output: Path, module: str, digest: str, packages: dict[str, str]
) -> dict[str, Any]:
    for name in ["validation.json", "executor-probe.json", "pytest.log", "junit.xml"]:
        required_file(output / name)
    probe = json.loads((output / "executor-probe.json").read_text())
    validate_probe(probe, packages)
    result: dict[str, Any] = json.loads((output / "validation.json").read_text())
    actual_junit = junit_results(output / "junit.xml", module)
    if (
        result.get("passed") is not True
        or type(result.get("pytest_exit_code")) is not int
        or result["pytest_exit_code"] != 0
        or result.get("python_executable") != "/opt/ci/bin/python"
        or result.get("module") != module
        or result.get("context_sha256") != digest
        or result.get("junit") != actual_junit
    ):
        raise ResourceFailure("Backend results differ from actual pinned execution")
    return result


def evidence_names(module: str) -> set[str]:
    names = {"containers.json"} | {name + "-cleanup.json" for name in CASES[module]}
    if module == "tests/lifecycle_runtime_backends.py":
        return names | {"accepted-image-shutdown.json"}
    names |= {name + ".json" for name in BACKENDS}
    if module == "tests/runtime_backends.py":
        return names | {name + ".json" for name in ("cancellation", *RECALL_EVIDENCE)}
    return names | {
        name + "-driver-binding.json" for name in (*BACKENDS, "cancellation")
    }


def validate_evidence_binding(value: Any, bindings: dict[str, str]) -> None:
    if not isinstance(value, dict) or not value:
        raise ResourceFailure("Missing driver source bindings")
    for path, digest in value.items():
        if not isinstance(path, str) or bindings.get("docker/core/" + path) != digest:
            raise ResourceFailure("Raw driver source binding differs")


def validate_client_cleanup(value: dict[str, Any]) -> None:
    if (
        value.get("pool_closed") is not True
        or value.get("http_closed") is not True
        or type(value.get("proxy_tasks")) is not int
        or value["proxy_tasks"] != 0
        or type(value.get("proxy_streams")) is not int
        or value["proxy_streams"] != 0
    ):
        raise ResourceFailure("Real client cleanup failed or leaked streams/tasks")


def validate_runtime_evidence(
    output: Path, module: str, bindings: dict[str, str], endpoints: dict[str, Any]
) -> dict[str, str]:
    from .core_source import inventory

    directory = output / "runtime"
    expected = evidence_names(module)
    if (
        not directory.is_dir()
        or directory.is_symlink()
        or {p.name for p in directory.iterdir()} != expected
    ):
        raise ResourceFailure("Missing or unexpected raw driver evidence")
    for name in sorted(expected):
        path = directory / name
        required_file(path)
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ResourceFailure("Malformed raw driver evidence")
        if name == "containers.json":
            if value != endpoints:
                raise ResourceFailure("Raw endpoints differ from owned containers")
        elif name.endswith("-cleanup.json"):
            validate_client_cleanup(value)
        elif name == "cancellation-driver-binding.json":
            if (
                value.get("driver") != module
                or value.get("driver_sha256") != bindings.get("docker/core/" + module)
                or value.get("health_sha256")
                != bindings.get("docker/core/app/observability/health_cache.py")
            ):
                raise ResourceFailure("Cancellation driver binding differs")
        else:
            validate_evidence_binding(value.get("source_sha256"), bindings)
            if name.endswith("-driver-binding.json") and value.get("driver") != module:
                raise ResourceFailure("Raw driver identity differs")
    return inventory(directory)
