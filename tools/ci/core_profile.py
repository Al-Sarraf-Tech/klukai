"""Closed container capability profiles and Python payload for full core CI."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import re
import sqlite3
from typing import Any

from .core_resources import ResourceFailure

# Executed by the image ENTRYPOINT /opt/ci/bin/python, never the host Python.
# Helpers come from the same read-only committed tree as the tested app.
DRIVER = r"""
import contextlib, io, json, pathlib, sqlite3, subprocess, sys, time, traceback
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
    result['probe_packages'] = len(probe['packages'])
    argv = [sys.executable, '-m', 'pytest', 'tests/', '-q', '-W', 'error',
            '-o', 'cache_dir=/tmp/pytest-cache',
            '--cov=app', '--cov-branch', '--cov-report=json:/evidence/coverage.json',
            '--cov-report=', '--junitxml=/evidence/junit.xml']
    result['pytest_command'] = argv
    start = time.monotonic()
    with (out / 'pytest.log').open('wb') as output:
        completed = subprocess.run(argv, cwd='/source/docker/core', stdout=output,
                                   stderr=subprocess.STDOUT, check=False)
    result['pytest_exit_code'] = completed.returncode
    result['pytest_seconds'] = time.monotonic() - start
    if completed.returncode != 0:
        raise ValueError('Full core pytest execution failed')
    # Recompute from retained SQLite under this same interpreter. Percentages
    # or an independently fabricated JSON report cannot replace observed arcs.
    from coverage import Coverage
    from tools.ci.core_profile import coverage_database
    from tools.ci.core_source import sha256
    database_path = out / '.coverage'
    observations = coverage_database(database_path)
    observed_digest = sha256(database_path)
    measured = Coverage(data_file='/evidence/.coverage', config_file='/source/docker/core/.coveragerc')
    measured.load()
    if not measured.get_data().has_arcs():
        raise ValueError('Coverage SQLite contains no branch observations')
    measured.json_report(outfile='/evidence/coverage-recomputed.json')
    data = json.loads((out / 'coverage.json').read_text())
    observed = json.loads((out / 'coverage-recomputed.json').read_text())
    if data['files'] != observed['files'] or data['totals'] != observed['totals']:
        raise ValueError('Coverage JSON differs from retained SQLite observations')
    from tools.ci.core_results import coverage_results, junit_results
    result['coverage'] = coverage_results(pathlib.Path('/source/docker/core'), data)
    if coverage_database(database_path) != observations or sha256(database_path) != observed_digest:
        raise ValueError('Retained coverage observations changed during validation')
    result['retained_coverage'] = {**observations, 'sha256': observed_digest}
    result['junit'] = junit_results(out / 'junit.xml')
    result['passed'] = True
except BaseException as error:
    result['error_type'] = type(error).__name__
    result['error'] = str(error)
    traceback.print_exc()
finally:
    (out / 'validation.json').write_text(json.dumps(result, indent=2) + '\n')
sys.exit(0 if result['passed'] else 1)
"""


def coverage_database(path: Path) -> dict[str, int]:
    """Inspect retained observations read-only and close the real SQLite handle."""
    try:
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ResourceFailure("Coverage SQLite integrity failed")
            if db.execute("SELECT value FROM meta WHERE key='has_arcs'").fetchone() != (
                "1",
            ):
                raise ResourceFailure("Coverage SQLite has no branch observations")
            counts = {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("file", "arc")
            }
            if not all(type(value) is int and value > 0 for value in counts.values()):
                raise ResourceFailure("Coverage SQLite has empty file/arc observations")
            return {"files": counts["file"], "arcs": counts["arc"]}
    except sqlite3.Error as error:
        raise ResourceFailure("Invalid retained coverage SQLite") from error


def safe_environment(password: str) -> dict[str, str]:
    env = {
        "CI": "1",
        "HOME": "/tmp/klukai-ci-home",
        "XDG_CACHE_HOME": "/tmp/cache",
        "PATH": "/opt/ci/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PYTHONPATH": "/source",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONWARNINGS": "error",
        "PYTEST_ADDOPTS": "",
        "COVERAGE_FILE": "/evidence/.coverage",
        "HYPOTHESIS_STORAGE_DIRECTORY": "/tmp/hypothesis",
        "KLUKAI_TEST_ALLOW_LIVE_BACKENDS": "0",
        "KLUKAI_CLAUDE_PRIMARY": "false",
        "KLUKAI_DISABLE_WARMUP": "1",
        "KLUKAI_IMAGE_PROVIDER": "disabled",
        "KLUKAI_CHAT_IMAGE_PROVIDER": "disabled",
        "KLUKAI_CREATIVE_TEXT_PROVIDER": "local",
        "KLUKAI_AGY_ALLOWED_USERS": "",
        "KLUKAI_AGY_SOCKET": "/tmp/disabled/image.sock",
        "KLUKAI_AGY_TOKEN_FILE": "/tmp/disabled/token",
        "KLUKAI_IMAGE_REFERENCE_FILES": "[]",
        "KLUKAI_BRAVE_ENABLED": "false",
        "KLUKAI_MCP_CONFIG": "",
        "ANTHROPIC_API_KEY": "",
        "OPENAI_API_KEY": "",
        "LM_STUDIO_TOKEN": "ci-dummy-never-used",
        "VOICE_BACKEND": "disabled",
        "VOICE_ASYNC_JOBS": "false",
        "DATABASE_URL": "postgresql://disabled:disabled@127.0.0.1:1/disabled",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "QDRANT_HOST": "127.0.0.1",
        "QDRANT_PORT": "1",
        "PERSONALITY_PATH": "/source/config/personality.yaml",
        "OTEL_SDK_DISABLED": "true",
        "KLUKAI_MIGRATION_TEST_DATABASE_URL": f"postgresql://core_test:{password}@postgres:5432/core_test",
    }
    env.update(
        {
            name: "http://127.0.0.1:1"
            for name in [
                "LM_STUDIO_URL",
                "VOICE_URL",
                "COMFYUI_URL",
                "MCP_URL",
                "QDRANT_URL",
            ]
        }
    )
    env.update(
        {
            f"KLUKAI_CLOUD_{kind}_{period}_LIMIT": "0"
            for kind in ["IMAGE", "TEXT"]
            for period in ["DAILY", "MONTHLY"]
        }
    )
    return env


def core_flags(source: Path, output: Path, network: str) -> list[str]:
    if source.is_relative_to(output) or output.is_relative_to(source):
        raise ResourceFailure("Source and writable evidence must be separate")
    return [
        "--user=1000:1000",
        "--read-only",
        "--cpus=2",
        "--memory=2g",
        "--memory-swap=2g",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=256",
        "--ipc=private",
        "--network=" + network,
        "--dns=127.0.0.1",
        "--workdir=/source/docker/core",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=512m,mode=1777",
        f"--mount=type=bind,source={source},target=/source,readonly",
        f"--mount=type=bind,source={output},target=/evidence",
    ]


def postgres_flags(network: str) -> list[str]:
    return [
        "--user=postgres",
        "--read-only",
        "--cpus=.5",
        "--memory=384m",
        "--memory-swap=384m",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=128",
        "--ipc=private",
        "--network=" + network,
        "--network-alias=postgres",
        "--dns=127.0.0.1",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777",
        "--tmpfs=/var/run/postgresql:rw,noexec,nosuid,nodev,size=16m,mode=1777",
        "--tmpfs=/var/lib/postgresql/data:rw,noexec,nosuid,nodev,size=256m,mode=1777",
    ]


def environment_flags(info: dict[str, Any], env: dict[str, str]) -> list[str]:
    # Empty every inherited image variable before supplying a closed allowlist.
    # The Docker CLI environment is also closed; no owner env files are used.
    names = set()
    for value in info["Config"].get("Env") or []:
        name = value.partition("=")[0]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ResourceFailure("Invalid image environment key")
        names.add(name)
    return ["--env=" + key + "=" for key in sorted(names - env.keys())] + [
        "--env=" + key for key in sorted(env)
    ]


def validate_core_profile(
    info: dict[str, Any],
    source: Path,
    output: Path,
    network: str,
    expected_env: dict[str, str],
) -> None:
    host = info["HostConfig"]
    expected = {
        "ReadonlyRootfs": True,
        "NanoCpus": 2_000_000_000,
        "Memory": 2 * 1024**3,
        "MemorySwap": 2 * 1024**3,
        "PidsLimit": 256,
        "Privileged": False,
        "NetworkMode": network,
        "Dns": ["127.0.0.1"],
        "IpcMode": "private",
        "PidMode": "",
    }
    if any(host.get(key) != value for key, value in expected.items()):
        raise ResourceFailure("Actual core isolation/resources differ")
    validate_capabilities(info, "1000:1000")
    validate_private_attachment(info, network)
    mounts = info.get("Mounts", [])
    observed = {
        (m.get("Type"), m.get("Source"), m.get("Destination"), m.get("RW"))
        for m in mounts
    }
    if len(mounts) != 2 or observed != {
        ("bind", str(source), "/source", False),
        ("bind", str(output), "/evidence", True),
    }:
        raise ResourceFailure("Unexpected source/evidence mount")
    tmpfs = host.get("Tmpfs") or {}
    if set(tmpfs) != {"/tmp"} or not {"noexec", "nosuid", "nodev"} <= set(
        tmpfs["/tmp"].split(",")
    ):
        raise ResourceFailure("Unsafe temporary filesystem")
    validate_environment(info, expected_env)


def validate_environment(info: dict[str, Any], expected_env: dict[str, str]) -> None:
    actual_env = dict(value.split("=", 1) for value in info["Config"].get("Env", []))
    if any(actual_env.get(key) != value for key, value in expected_env.items()):
        raise ResourceFailure("Actual core environment differs")
    if any(value for key, value in actual_env.items() if key not in expected_env):
        raise ResourceFailure("Unexpected inherited container environment")


def validate_capabilities(info: dict[str, Any], user: str) -> None:
    host = info["HostConfig"]
    if info["Config"].get("User") != user or host.get("CapDrop") != ["ALL"]:
        raise ResourceFailure("Actual core user/capabilities differ")
    if "no-new-privileges:true" not in host.get("SecurityOpt", []):
        raise ResourceFailure("Missing no-new-privileges")
    if any(
        host.get(key)
        for key in ["Devices", "DeviceRequests", "PortBindings", "PublishAllPorts"]
    ):
        raise ResourceFailure("Unexpected device/GPU/port capability")


def validate_postgres_profile(
    info: dict[str, Any] | None, network: str, env: dict[str, str]
) -> None:
    if info is None:
        raise ResourceFailure("PostgreSQL container disappeared")
    expected = {
        "ReadonlyRootfs": True,
        "NanoCpus": 500_000_000,
        "Memory": 384 * 1024**2,
        "MemorySwap": 384 * 1024**2,
        "PidsLimit": 128,
        "Privileged": False,
        "NetworkMode": network,
        "Dns": ["127.0.0.1"],
        "IpcMode": "private",
        "PidMode": "",
    }
    if any(info["HostConfig"].get(key) != value for key, value in expected.items()):
        raise ResourceFailure("Actual PostgreSQL isolation/resources differ")
    validate_capabilities(info, "postgres")
    validate_private_attachment(info, network)
    validate_environment(info, env)
    if info.get("Mounts"):
        raise ResourceFailure("PostgreSQL must have no host/volume mounts")
    temporary = info["HostConfig"].get("Tmpfs") or {}
    if set(temporary) != {
        "/tmp",
        "/var/run/postgresql",
        "/var/lib/postgresql/data",
    } or any(
        not {"noexec", "nosuid", "nodev"} <= set(value.split(","))
        for value in temporary.values()
    ):
        raise ResourceFailure("Unsafe PostgreSQL temporary filesystems")


def validate_private_attachment(info: dict[str, Any], network: str) -> None:
    networks = info.get("NetworkSettings", {}).get("Networks") or {}
    if set(networks) != {network} or any(
        endpoint.get(key)
        for endpoint in networks.values()
        for key in ("Gateway", "IPv6Gateway")
    ):
        raise ResourceFailure("Container has an unexpected network or external gateway")
