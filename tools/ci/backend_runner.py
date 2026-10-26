"""Run the real-backend CI cases in pinned CPython with host-owned services."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys
from typing import Any

from . import (
    backend_profile,
    backend_services,
    cancellation,
    core_profile,
    core_resources,
    core_runner,
    core_source,
    python_executor,
)
from .cancellation import SignalLatch, cancellation_scope
from .core_profile import environment_flags
from .core_resources import Docker, ResourceFailure, Resources
from .core_source import (
    SourceFailure,
    inventory,
    reserve_output,
    sha256,
    snapshot,
    unchanged,
    write_json,
)
from .python_executor import lock_versions, validate_image


@dataclass(frozen=True)
class RunConfig:
    root: Path
    output: Path
    revision: str
    image: str
    postgres_image: str
    redis_image: str
    development: bool = False
    timeout: float = 300


def producer_hashes(captured: dict[str, Any]) -> dict[str, str]:
    modules = [
        sys.modules[__name__],
        backend_profile,
        backend_services,
        cancellation,
        core_profile,
        core_resources,
        core_runner,
        core_source,
        python_executor,
    ]
    hashes = {}
    for module in modules:
        filename = module.__file__
        if not filename:
            raise SourceFailure("Producer helper has no file identity")
        actual = Path(filename)
        name = "tools/ci/" + actual.name
        if not actual.is_file() or any(
            p.is_symlink() for p in (actual, *actual.parents)
        ):
            raise SourceFailure("Producer helper is missing or linked")
        digest = sha256(actual)
        if captured["files"].get(name) != digest:
            raise SourceFailure(
                "Producer helper differs from committed source snapshot"
            )
        hashes[name] = digest
    return hashes


def context_document(
    captured: dict[str, Any],
    module: str,
    resources: Resources,
    services: dict[str, Any],
    password: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "token": resources.token,
        "revision": captured["revision"],
        "module": module,
        "source_sha256": captured["files"],
        "password": password,
        "backends": {
            name: {key: row[key] for key in ("host", "port", "image_id")}
            for name, row in services.items()
        },
    }


def run_module(
    config: RunConfig,
    captured: dict[str, Any],
    module: str,
    images: dict[str, dict[str, Any]],
    packages: dict[str, str],
    latch: SignalLatch,
) -> dict[str, Any]:
    name = Path(module).stem
    output = config.output / "modules" / name
    output.mkdir(mode=0o700, parents=True)
    docker = Docker(output)
    resources = Resources(docker, output)
    result: dict[str, Any] = {"module": module, "passed": False}
    try:
        resources.create_network()
        password = secrets.token_urlsafe(32)
        docker.secrets.append(password)
        result["services"] = backend_services.start_services(
            resources, images, password
        )
        context = config.output / "contexts" / name
        context.mkdir(mode=0o700, parents=True)
        context_path = context / "context.json"
        write_json(
            context_path,
            context_document(captured, module, resources, result["services"], password),
        )
        context_path.chmod(0o400)
        digest = sha256(context_path)
        result["context_sha256"] = digest
        results = output / "results"
        results.mkdir(mode=0o700)
        (results / "runtime").mkdir(mode=0o700)
        env = backend_profile.worker_environment(module, digest)
        flags = backend_profile.worker_flags(
            config.output / "source", results, context, resources.network
        )
        flags += environment_flags(images["core"], env)
        cid = resources.create_container(
            "worker",
            images["core"]["Id"],
            flags,
            environment=env,
            command=["-W", "error", "-c", backend_profile.DRIVER],
        )
        info = docker.container(cid)
        if (
            info is None
            or info.get("Id") != cid
            or info.get("Image") != images["core"]["Id"]
        ):
            raise ResourceFailure("Worker image/identity differs")
        backend_profile.validate_worker(
            info, config.output / "source", results, context, resources.network, env
        )
        result["worker_id"] = cid
        result["execution"] = resources.attach(cid, timeout=config.timeout)
        if any(value != 0 for value in result["execution"].values()):
            raise ResourceFailure("Real-backend worker execution failed")
        result["validation"] = backend_profile.load_validation(
            results, module, digest, packages
        )
        endpoints = {
            name: {key: row[key] for key in ("host", "port", "image_id")}
            for name, row in result["services"].items()
        }
        result["runtime_sha256"] = backend_profile.validate_runtime_evidence(
            results, module, captured["files"], endpoints
        )
        if sha256(context_path) != digest:
            raise SourceFailure("Readonly managed context changed during execution")
        result["passed"] = True
    except (Exception, KeyboardInterrupt) as error:
        result["error_type"], result["error"] = (
            type(error).__name__,
            docker.scrub(str(error)),
        )
    finally:
        with latch.deferred():
            for resource in resources.state["containers"]:
                try:
                    info = resources.owned(resource)
                    if info is not None:
                        docker.run("logs", info["Id"], timeout=15)
                except (Exception, KeyboardInterrupt) as error:
                    result.setdefault("log_errors", []).append(docker.scrub(str(error)))
                    result["passed"] = False
            try:
                resources.close()
                result["resources"] = resources.state
            except (Exception, KeyboardInterrupt) as error:
                result["cleanup_error"] = docker.scrub(str(error))
                result["passed"] = False
            latch.write_report(output / "module.json", result)
    return result


def execute(config: RunConfig) -> dict[str, Any]:
    with cancellation_scope() as latch:
        return _execute(config, latch)


def _execute(config: RunConfig, latch: SignalLatch) -> dict[str, Any]:
    root, output = config.root.resolve(), config.output
    reserve_output(root, output)
    report: dict[str, Any] = {
        "schema_version": 1,
        "source_revision": config.revision,
        "passed": False,
        "certifying": False,
        "development": config.development,
        "source_unchanged": False,
        "modules": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    captured = None
    try:
        if not 0 < config.timeout <= 900:
            raise ResourceFailure("Per-module timeout must be bounded to900seconds")
        captured = snapshot(
            root, output, config.revision, development=config.development
        )
        report["source"] = captured
        report["producer_sha256"] = producer_hashes(captured)
        lock_text = (output / "source/docker/core/requirements-ci.txt").read_text()
        lock_sha = hashlib.sha256(lock_text.encode()).hexdigest()
        packages = lock_versions(lock_text)
        docker = Docker(output)
        images = {
            name: core_runner.image_info(docker, identity)
            for name, identity in {
                "core": config.image,
                "postgres": config.postgres_image,
                "redis": config.redis_image,
            }.items()
        }
        validate_image(
            images["core"],
            images["core"]["Id"],
            "unverified" if config.development else config.revision,
            lock_sha,
            development=config.development,
        )
        if "PG_MAJOR=17" not in images["postgres"]["Config"].get("Env", []):
            raise ResourceFailure("PostgreSQL image must be17")
        report["images"] = {name: value["Id"] for name, value in images.items()}
        report["lock_sha256"] = lock_sha
        report["docker_version"] = json.loads(
            docker.run("version", "--format={{json .}}").stdout
        )
        (output / "driver.py").write_text(backend_profile.DRIVER)
        for module in backend_profile.CASES:
            latch.checkpoint()
            result = run_module(config, captured, module, images, packages, latch)
            report["modules"].append(result)
            if not result["passed"]:
                raise ResourceFailure(
                    "Real-backend module failed; inspect private evidence"
                )
        report["passed"] = True
    except (Exception, KeyboardInterrupt) as error:
        report["error_type"], report["error"] = type(error).__name__, str(error)
    finally:
        with latch.deferred():
            if captured is not None:
                try:
                    unchanged(root, output, captured)
                    if producer_hashes(captured) != report.get("producer_sha256"):
                        raise SourceFailure(
                            "Executing producer changed during backend gate"
                        )
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
            latch.write_report(output / "manifest.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["root", "output"]:
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ["revision", "image", "postgres-image", "redis-image"]:
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--timeout", type=float, default=300)
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
        print(f"Managed backend CI failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
