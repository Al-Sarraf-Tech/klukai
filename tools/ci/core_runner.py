"""Run full core CI in its immutable CPython image and a disposable private PG."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from typing import Any

from .cancellation import SignalLatch, cancellation_scope
from .core_profile import (
    DRIVER,
    core_flags,
    environment_flags,
    postgres_flags,
    safe_environment,
    validate_core_profile,
    validate_postgres_profile,
)
from .core_resources import Docker, ResourceFailure, Resources
from .core_source import (
    SourceFailure,
    inventory,
    reserve_output,
    sha256,
    snapshot,
    unchanged,
)
from .python_executor import lock_versions, validate_image


@dataclass(frozen=True)
class RunConfig:
    root: Path
    output: Path
    revision: str
    image: str
    postgres_image: str
    development: bool = False
    timeout: float = 1200


def image_info(docker: Docker, reference: str) -> dict[str, Any]:
    if not re.fullmatch(
        r"(?:sha256:[a-f0-9]{64}|[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64})", reference
    ):
        raise ResourceFailure(
            "Only immutable full cached image identities are accepted"
        )
    raw = docker.run("image", "inspect", reference, private_stdout=True).stdout
    rows = json.loads(raw)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ResourceFailure("Expected exactly one cached image")
    info: dict[str, Any] = rows[0]
    identity = info.get("Id", "")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", identity):
        raise ResourceFailure("Missing actual image ID")
    if reference.startswith("sha256:") and identity != reference:
        raise ResourceFailure("Resolved image ID mismatch")
    if "@" in reference and reference not in info.get("RepoDigests", []):
        raise ResourceFailure("Resolved repository digest mismatch")
    return info


def ready_postgres(docker: Docker, cid: str, password: str) -> str:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = docker.run(
            "exec",
            cid,
            "pg_isready",
            "-h",
            "postgres",
            "-U",
            "core_test",
            "-d",
            "core_test",
            check=False,
        )
        if result.returncode == 0:
            break
        time.sleep(0.3)
    else:
        raise ResourceFailure("Disposable PostgreSQL readiness timed out")
    version = docker.run(
        "exec",
        "--env=PGPASSWORD",
        cid,
        "psql",
        "-h",
        "postgres",
        "-U",
        "core_test",
        "-d",
        "core_test",
        "-Atc",
        "SELECT current_setting('server_version_num')",
        environment={"PGPASSWORD": password},
    ).stdout.strip()
    if not version.isdigit() or not 170000 <= int(version) < 180000:
        raise ResourceFailure("Actual PostgreSQL major version is not17")
    uid = docker.run("exec", cid, "id", "-u").stdout.strip()
    if not uid.isdigit() or int(uid) == 0:
        raise ResourceFailure("PostgreSQL is not nonroot")
    return version


def load_validation(output: Path, expected_packages: dict[str, str]) -> dict[str, Any]:
    from .python_executor import validate_probe

    for name in [
        "executor-probe.json",
        "validation.json",
        "pytest.log",
        "coverage.json",
        "coverage-recomputed.json",
        ".coverage",
        "junit.xml",
    ]:
        path = output / name
        if not path.is_file() or path.is_symlink() or not path.stat().st_size:
            raise ResourceFailure("Missing/linked/empty core execution output")
    probe = json.loads((output / "executor-probe.json").read_text())
    validate_probe(probe, expected_packages)
    result: dict[str, Any] = json.loads((output / "validation.json").read_text())
    if (
        result.get("passed") is not True
        or type(result.get("pytest_exit_code")) is not int
        or result["pytest_exit_code"] != 0
        or result.get("python_executable") != "/opt/ci/bin/python"
    ):
        raise ResourceFailure(
            "Core results were not successfully validated in the pinned interpreter"
        )
    validate_summary(result)
    return result


def validate_summary(result: dict[str, Any]) -> None:
    coverage = result.get("coverage") or {}
    for key in ("statements_percent", "branches_percent"):
        value = coverage.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 95 <= value <= 100
        ):
            raise ResourceFailure("Missing or insufficient statement/branch results")
    junit = result.get("junit") or {}
    if any(type(junit.get(key)) is not int for key in ("passed", "skipped", "tests")):
        raise ResourceFailure("Missing actual JUnit counts")
    if not (
        junit["passed"] > 0
        and 0 <= junit["skipped"] <= 75
        and junit["tests"] == junit["passed"] + junit["skipped"]
    ):
        raise ResourceFailure("Empty, excessive skipped or inconsistent JUnit results")


def producer_hashes(captured: dict[str, Any]) -> dict[str, str]:
    result = {}
    for filename in (
        "core_runner.py",
        "core_source.py",
        "core_resources.py",
        "core_profile.py",
        "python_executor.py",
        "cancellation.py",
    ):
        actual = Path(__file__).with_name(filename)
        name = "tools/ci/" + filename
        if actual.is_symlink() or not actual.is_file():
            raise SourceFailure("Producer helper is missing or linked")
        digest = sha256(actual)
        if captured["files"].get(name) != digest:
            raise SourceFailure(
                "Producer helpers differ from the committed source snapshot"
            )
        result[name] = digest
    return result


def execute(config: RunConfig) -> dict[str, Any]:
    with cancellation_scope() as cancellation:
        return _execute(config, cancellation)


def _execute(config: RunConfig, cancellation: SignalLatch) -> dict[str, Any]:
    root, output = config.root.resolve(), config.output
    reserve_output(root, output)
    docker = Docker(output)
    resources: Resources | None = None
    report: dict[str, Any] = {
        "schema_version": 1,
        "source_revision": config.revision,
        "certifying": False,
        "passed": False,
        "development": config.development,
        "source_unchanged": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "quota": {"cpus": 2, "memory_bytes": 2 * 1024**3},
    }
    captured: dict[str, Any] | None = None
    try:
        if not 0 < config.timeout <= 1800:
            raise ResourceFailure("Timeout must be bounded to at most1800seconds")
        captured = snapshot(
            root, output, config.revision, development=config.development
        )
        report["source"] = captured
        report["producer_sha256"] = producer_hashes(captured)
        lock_text = (output / "source/docker/core/requirements-ci.txt").read_text()
        lock_sha = hashlib.sha256(lock_text.encode()).hexdigest()
        expected_packages = lock_versions(lock_text)
        tool_image, pg_image = (
            image_info(docker, config.image),
            image_info(docker, config.postgres_image),
        )
        validate_image(
            tool_image,
            tool_image["Id"],
            "unverified" if config.development else config.revision,
            lock_sha,
            development=config.development,
        )
        if "PG_MAJOR=17" not in pg_image["Config"].get("Env", []):
            raise ResourceFailure("PostgreSQL image must be17")
        report.update(
            {
                "source": captured,
                "lock_sha256": lock_sha,
                "images": {
                    "core": {
                        "input": config.image,
                        "id": tool_image["Id"],
                        "config": {
                            k: v for k, v in tool_image["Config"].items() if k != "Env"
                        },
                    },
                    "postgres": {"input": config.postgres_image, "id": pg_image["Id"]},
                },
            }
        )
        report["docker_version"] = json.loads(
            docker.run("version", "--format={{json .}}").stdout
        )
        resources = Resources(docker, output)
        resources.create_network()
        password = secrets.token_urlsafe(24)
        docker.secrets.append(password)
        pg_env = {
            "POSTGRES_USER": "core_test",
            "POSTGRES_DB": "core_test",
            "POSTGRES_PASSWORD": password,
            "PGDATA": "/var/lib/postgresql/data/pgdata",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PG_MAJOR": "17",
            "LANG": "en_US.utf8",
        }
        pg = resources.create_container(
            "postgres",
            pg_image["Id"],
            postgres_flags(resources.network) + environment_flags(pg_image, pg_env),
            environment=pg_env,
        )
        docker.run("start", pg)
        report["postgres_server_version_num"] = ready_postgres(docker, pg, password)
        validate_postgres_profile(docker.container(pg), resources.network, pg_env)
        env = safe_environment(password)
        results = output / "results"
        results.mkdir(mode=0o700)
        flags = core_flags(
            output / "source", results, resources.network
        ) + environment_flags(tool_image, env)
        (output / "driver.py").write_text(DRIVER)
        cid = resources.create_container(
            "core",
            tool_image["Id"],
            flags,
            environment=env,
            command=["-W", "error", "-c", DRIVER],
        )
        info = docker.container(cid)
        if info is None:
            raise ResourceFailure("Created core container disappeared")
        validate_core_profile(info, output / "source", results, resources.network, env)
        report["environment"] = {key: docker.scrub(value) for key, value in env.items()}
        report["execution"] = resources.attach(cid, timeout=config.timeout)
        if any(value != 0 for value in report["execution"].values()):
            raise ResourceFailure("Core container execution failed")
        report["results"] = load_validation(results, expected_packages)
        report["passed"] = True
    except (Exception, KeyboardInterrupt) as error:
        report["error_type"], report["error"] = (
            type(error).__name__,
            docker.scrub(str(error)),
        )
    finally:
        with cancellation.deferred():
            if resources is not None:
                try:
                    resources.close()
                except (Exception, KeyboardInterrupt) as error:
                    report["cleanup_error"] = docker.scrub(str(error))
                    report["passed"] = False
            if captured is not None:
                try:
                    unchanged(root, output, captured)
                    report["source_unchanged"] = True
                except (Exception, KeyboardInterrupt) as error:
                    report["source_error"] = str(error)
                    report["passed"] = False
            report["certifying"] = report["passed"] and not config.development
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            try:
                report["raw_sha256"] = {
                    name: digest
                    for name, digest in inventory(output).items()
                    if not name.startswith(("source/", "docker-config/"))
                    and name not in {"source.tar", "manifest.json"}
                }
            except (OSError, SourceFailure) as error:
                report["evidence_error"] = str(error)
                report["passed"] = report["certifying"] = False
            cancellation.write_report(output / "manifest.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["root", "output"]:
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ["revision", "image", "postgres-image"]:
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--development", action="store_true")
    args = parser.parse_args()
    try:
        report = execute(RunConfig(**vars(args)))
        print(
            json.dumps(
                {
                    "passed": report["passed"],
                    "certifying": report["certifying"],
                    "output": str(args.output),
                }
            )
        )
        return 0 if report["passed"] else 1
    except (
        SourceFailure,
        ResourceFailure,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Core CI failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
