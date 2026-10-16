"""Build and certify one exact-source CPython CI tool image from five materials."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from typing import Any

from . import cancellation, core_resources, core_source, python_executor, tool_materials
from .cancellation import SignalLatch, cancellation_scope
from .core_resources import Docker, ResourceFailure
from .core_source import require, sha256, write_json


@dataclass(frozen=True)
class RunConfig:
    root: Path
    output: Path
    revision: str
    timeout: float = 1800


def executing_helpers(captured: dict[str, Any]) -> dict[str, dict[str, str]]:
    result = {}
    for module in [
        sys.modules[__name__],
        tool_materials,
        core_source,
        core_resources,
        cancellation,
        python_executor,
    ]:
        require(
            isinstance(module.__file__, str), "Executing helper has no file identity"
        )
        path = Path(str(module.__file__))
        require(
            path.is_file() and not any(p.is_symlink() for p in (path, *path.parents)),
            "Executing helper is missing or linked",
        )
        name = "tools/ci/" + path.name
        digest = sha256(path)
        require(
            captured["files"].get(name) == digest,
            "Executing helper differs from complete committed source",
        )
        result[name] = {"path": str(path.absolute()), "sha256": digest}
    return result


def regular(path: Path, *, limit: int = 2 * 1024**2) -> None:
    require(
        path.is_file() and not any(p.is_symlink() for p in (path, *path.parents)),
        "Missing/linked build execution evidence",
    )
    require(
        path.stat().st_nlink == 1 and 0 < path.stat().st_size <= limit,
        "Empty/linked/oversized build execution evidence",
    )


def read_iid(path: Path) -> str:
    regular(path, limit=72)
    value = path.read_text()
    require(
        re.fullmatch(r"sha256:[a-f0-9]{64}\n?", value),
        "Build did not emit a full immutable image ID",
    )
    return value.strip()


def save_streams(output: Path, prefix: str, result: Any) -> None:
    for stream in ["stdout", "stderr"]:
        value = getattr(result, stream, "") or ""
        if isinstance(value, bytes):
            value = value.decode(errors="replace")
        with (output / f"{prefix}.{stream}").open("x") as destination:
            destination.write(str(value))


def reap_client(
    process: subprocess.Popen[bytes], report: dict[str, Any], latch: SignalLatch
) -> None:
    with latch.deferred():
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        report["build"]["client_exit_code"] = process.returncode
        report["build"]["client_reaped"] = process.returncode is not None


def run_build(
    command: list[str],
    environment: dict[str, str],
    output: Path,
    timeout: float,
    report: dict[str, Any],
    latch: SignalLatch,
) -> subprocess.CompletedProcess[bytes]:
    # Stream directly to fresh private files: interrupting a buffered run() can
    # discard the compiler's partial output. Only this CLI process is owned;
    # its exit after cancellation does not establish daemon build completion.
    with (
        (output / "build.stdout").open("xb") as stdout,
        (output / "build.stderr").open("xb") as stderr,
    ):
        process: subprocess.Popen[bytes] | None = None
        failure: BaseException | None = None
        try:
            with latch.deferred():
                process = subprocess.Popen(
                    command,
                    stdout=stdout,
                    stderr=stderr,
                    env=environment,
                    start_new_session=True,
                )
                report["build"]["client_pid"] = process.pid
            latch.checkpoint()
            code = process.wait(timeout=timeout)
            report["build"].update({"completion_known": code >= 0, "exit_code": code})
            return subprocess.CompletedProcess(command, code)
        except (Exception, KeyboardInterrupt) as error:
            failure = error
            raise
        finally:
            if process is not None:
                try:
                    reap_client(process, report, latch)
                except (Exception, KeyboardInterrupt) as error:
                    report["build"]["client_cleanup_error"] = str(error)
                    if failure is None:
                        raise


def build_image(
    config: RunConfig,
    docker: Docker,
    materials: dict[str, Any],
    report: dict[str, Any],
    latch: SignalLatch,
) -> str:
    output = config.output
    command = [
        "build",
        "--pull=false",
        "--progress=plain",
        "--platform=linux/amd64",
        "--file=" + str(output / "context/Dockerfile.ci"),
        "--iidfile=" + str(output / "image.iid"),
        "--build-arg=VCS_REF=" + config.revision,
        "--build-arg=CI_LOCK_SHA256="
        + materials["context_sha256"]["requirements-ci.txt"],
        "--build-arg=SOURCE_DATE_EPOCH=" + str(materials["source_date_epoch"]),
        str(output / "context"),
    ]
    report["build"] = {
        "argv": ["docker", *command],
        "completion_known": False,
        "exit_code": None,
        "daemon_cache_owned": False,
    }
    result = run_build(
        ["docker", *command], docker.environment, output, config.timeout, report, latch
    )
    report["build"].update(
        {"completion_known": result.returncode >= 0, "exit_code": result.returncode}
    )
    if result.returncode != 0:
        raise ResourceFailure("CI tool build failed; inspect private raw build output")
    return read_iid(output / "image.iid")


def inspect_image(
    docker: Docker, identity: str, revision: str, lock: str
) -> dict[str, Any]:
    result = docker.run("image", "inspect", identity)
    rows = json.loads(result.stdout)
    require(
        isinstance(rows, list) and len(rows) == 1, "Expected one actual built image"
    )
    python_executor.validate_image(rows[0], identity, revision, lock)
    return dict(rows[0])


def probe_image(
    identity: str,
    output: Path,
    packages: dict[str, str],
    report: dict[str, Any],
    latch: SignalLatch,
) -> None:
    evidence = output / "probe"
    evidence.mkdir(mode=0o700)
    result: Any = None
    failure: BaseException | None = None
    try:
        result = python_executor.probe_container(identity, evidence, timeout=120)
        require(result.returncode == 0, "Native CI tool probe returned a nonzero exit")
        regular(evidence / "probe-manifest.json")
        manifest = json.loads((evidence / "probe-manifest.json").read_text())
        require(
            isinstance(manifest, dict)
            and manifest.get("passed") is True
            and not manifest.get("cleanup_error"),
            "Native CI tool probe/owned cleanup failed",
        )
        probe = json.loads(result.stdout)
        python_executor.validate_probe(probe, packages)
        report["probe"] = probe
        report["probe_exit_code"] = result.returncode
    except (Exception, KeyboardInterrupt) as error:
        failure = error
        raise
    finally:
        with latch.deferred():
            try:
                save_streams(
                    evidence, "probe", result if result is not None else failure
                )
            except (Exception, KeyboardInterrupt) as error:
                report["probe_stream_error"] = str(error)
                if failure is None:
                    raise


def close_evidence(
    config: RunConfig,
    captured: dict[str, Any] | None,
    materials: dict[str, Any] | None,
    report: dict[str, Any],
) -> None:
    checks: list[tuple[str, Callable[[], None]]] = []
    if captured is not None:
        checks.append(
            (
                "source",
                lambda: core_source.unchanged(
                    config.root.resolve(), config.output, captured
                ),
            )
        )
        checks.append(
            (
                "helper",
                lambda: require(
                    executing_helpers(captured) == report.get("executing_helpers"),
                    "Executing helper identity changed during build",
                ),
            )
        )
    if materials is not None and captured is not None:
        checks.append(
            (
                "material",
                lambda: tool_materials.unchanged(
                    config.root.resolve(), config.output, captured, materials
                ),
            )
        )
    if "image_id" in report:
        checks.append(
            (
                "iid",
                lambda: require(
                    read_iid(config.output / "image.iid") == report["image_id"],
                    "Built image IID changed during verification",
                ),
            )
        )
    for name, check in checks:
        try:
            check()
            report[name + "_unchanged"] = True
        except (Exception, KeyboardInterrupt) as error:
            report.setdefault("closing_errors", {})[name] = str(error)
            report["passed"] = False
            report.setdefault("error_type", type(error).__name__)
            report.setdefault("error", str(error))
    path = config.output / "probe/probe-manifest.json"
    if path.exists() or path.is_symlink():
        try:
            regular(path)
            report["probe_execution"] = json.loads(path.read_text())
        except (Exception, KeyboardInterrupt) as error:
            report["probe_evidence_error"] = str(error)
            report["passed"] = False


def execute(config: RunConfig) -> dict[str, Any]:
    with cancellation_scope() as latch:
        return _execute(config, latch)


def _execute(config: RunConfig, latch: SignalLatch) -> dict[str, Any]:
    root, output = config.root.resolve(), config.output
    core_source.reserve_output(root, output)
    report: dict[str, Any] = {
        "schema_version": 1,
        "passed": False,
        "certifying": False,
        "source_revision": config.revision,
        "source_unchanged": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    captured = materials = None
    try:
        require(
            0 < config.timeout <= 3600,
            "Build timeout must be bounded to at most3600seconds",
        )
        captured = core_source.snapshot(
            root, output, config.revision, development=False
        )
        report["source"] = captured
        report["executing_helpers"] = executing_helpers(captured)
        materials = tool_materials.prepare(root, output, captured)
        report["materials"] = materials
        write_json(output / "materials.json", materials)
        docker = Docker(output)
        identity = build_image(config, docker, materials, report, latch)
        report["image_id"] = identity
        image = inspect_image(
            docker,
            identity,
            config.revision,
            materials["context_sha256"]["requirements-ci.txt"],
        )
        report["image"] = image
        probe_image(identity, output, materials["packages"], report, latch)
        report["passed"] = True
    except (Exception, KeyboardInterrupt) as error:
        report["error_type"], report["error"] = type(error).__name__, str(error)
    finally:
        with latch.deferred():
            close_evidence(config, captured, materials, report)
            report["certifying"] = report["passed"]
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            try:
                report["raw_sha256"] = {
                    name: digest
                    for name, digest in core_source.inventory(output).items()
                    if not name.startswith(("source/", "docker-config/"))
                    and name not in {"source.tar", "manifest.json"}
                }
            except (Exception, KeyboardInterrupt) as error:
                report["evidence_error"] = str(error)
                report["passed"] = report["certifying"] = False
                report.setdefault("error_type", type(error).__name__)
                report.setdefault("error", str(error))
            latch.write_report(output / "manifest.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["root", "output"]:
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    try:
        report = execute(RunConfig(**vars(args)))
        print(
            json.dumps(
                {
                    "passed": report["passed"],
                    "certifying": report["certifying"],
                    "image_id": report.get("image_id"),
                    "output": str(args.output),
                }
            )
        )
        return 0 if report["certifying"] else 1
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"CI tool image build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
