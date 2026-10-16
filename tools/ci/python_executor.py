"""Verify a hash-locked CI image by executing its actual Python environment."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from tools.ci import cancellation as cancellation_module
from tools.ci import core_resources as resources_module
from tools.ci import core_source as source_module
from tools.ci.cancellation import Cancelled, SignalLatch, cancellation_scope
from tools.ci.core_resources import Docker, Resources
from tools.ci.core_source import git, inventory, reserve_output, sha256, write_json

PROBE = """import importlib.metadata as m, json, platform, re, ssl
import PIL.Image, psycopg, uvloop
normalize=lambda s: re.sub(r'[-_.]+','-',s).lower()
print(json.dumps({'python_version':platform.python_version(),
'packages':{normalize(d.metadata['Name']):d.version for d in m.distributions()},
'openssl':ssl.OPENSSL_VERSION,'libc':platform.libc_ver(),
'machine':platform.machine(),'psycopg_impl':psycopg.pq.__impl__}))
"""


class InvalidExecutor(ValueError):
    """Missing, stale or mismatched execution evidence."""


def lock_versions(text: str) -> dict[str, str]:
    result = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "--hash=sha256:")):
            continue
        match = re.fullmatch(
            r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*\])?"
            r"==([A-Za-z0-9.!+_-]+)\s*\\?",
            line,
        )
        if match is None:
            raise InvalidExecutor("CI inputs must be an exclusive pinned lock")
        name = re.sub(r"[-_.]+", "-", match[1]).lower()
        if name in result:
            raise InvalidExecutor("duplicate CI package identity")
        result[name] = match[2]
    if not result:
        raise InvalidExecutor("empty CI package lock")
    return result


def validate_probe(probe: Any, expected: dict[str, str]) -> None:
    if not isinstance(probe, dict) or probe.get("python_version") != "3.14.8":
        raise InvalidExecutor("CI interpreter differs from production CPython")
    if not expected or probe.get("packages") != expected:
        raise InvalidExecutor("executed CI packages differ from the complete lock")


def validate_image(
    info: Any, identity: str, revision: str, lock: str, *, development: bool = False
) -> None:
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", identity):
        raise InvalidExecutor("CI executor requires an immutable full image ID")
    if not re.fullmatch(r"[a-f0-9]{64}", lock):
        raise InvalidExecutor("CI executor requires the complete lock digest")
    if not re.fullmatch(r"[a-f0-9]{40}", revision) and not (
        development and revision == "unverified"
    ):
        raise InvalidExecutor("CI executor requires the exact source revision")
    if not isinstance(info, dict):
        raise InvalidExecutor("invalid executor image metadata")
    config = info.get("Config")
    if not isinstance(config, dict):
        raise InvalidExecutor("missing executor image configuration")
    labels = config.get("Labels")
    if (
        info.get("Id") != identity
        or info.get("Architecture") != "amd64"
        or info.get("Os") != "linux"
        or config.get("User") != "1000:1000"
        or config.get("Entrypoint") != ["/opt/ci/bin/python"]
        or not isinstance(labels, dict)
        or labels.get("org.opencontainers.image.revision") != revision
        or labels.get("cc.appnest.klukai.ci-lock-sha256") != lock
    ):
        raise InvalidExecutor("CI image identity, source, lock or isolation differs")


INPUTS = (
    "docker/core/Dockerfile.ci",
    "docker/core/Dockerfile.ci.dockerignore",
    "docker/core/requirements-build.txt",
    "docker/core/requirements-ci.txt",
    "docker/core/package_runtime.py",
    "tools/ci/python_executor.py",
    "tools/ci/cancellation.py",
    "tools/ci/core_resources.py",
    "tools/ci/core_source.py",
)


def source_identity(root: Path) -> dict[str, Any]:
    names = git(root, "ls-files", "-z", "--", *INPUTS).rstrip("\0").split("\0")
    if set(names) != set(INPUTS):
        raise InvalidExecutor("Executor committed input set differs")
    inputs = {}
    for name in INPUTS:
        path = root / name
        if not path.is_file() or any(
            parent.is_symlink() for parent in (path, *path.parents)
        ):
            raise InvalidExecutor("Executor input is missing or linked")
        inputs[name] = sha256(path)
    return {
        "head": git(root, "rev-parse", "HEAD"),
        "tree": git(root, "rev-parse", "HEAD^{tree}"),
        "status": git(root, "status", "--porcelain", "--untracked-files=all"),
        "inputs": inputs,
    }


def executing_helpers(root: Path, source: dict[str, Any]) -> dict[str, dict[str, str]]:
    locations = {
        "tools/ci/python_executor.py": __file__,
        "tools/ci/cancellation.py": cancellation_module.__file__,
        "tools/ci/core_resources.py": resources_module.__file__,
        "tools/ci/core_source.py": source_module.__file__,
    }
    result = {}
    for name, filename in locations.items():
        if not isinstance(filename, str):
            raise InvalidExecutor(f"Executing helper has no file identity: {name}")
        path = Path(filename)
        if not path.is_file() or any(
            parent.is_symlink() for parent in (path, *path.parents)
        ):
            raise InvalidExecutor(f"Executing helper is missing or linked: {name}")
        digest = sha256(path)
        committed = hashlib.sha256(
            subprocess.check_output(
                ["git", "-C", str(root), "show", source["head"] + ":" + name]
            )
        ).hexdigest()
        if digest != committed or source["inputs"][name] != committed:
            raise InvalidExecutor(
                f"Executing helper differs from committed selected source: {name}"
            )
        result[name] = {"path": str(path.absolute()), "sha256": digest}
    return result


def probe_container(
    image: str, output: Path, *, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    """Probe native imports; preserve the original failure and await owned cleanup."""
    with cancellation_scope() as cancellation:
        return _probe_container(image, output, timeout, cancellation)


def _probe_container(
    image: str, output: Path, timeout: float, cancellation: SignalLatch
) -> subprocess.CompletedProcess[str]:
    docker = Docker(output)
    resources = Resources(docker, output, network_required=False)
    report: dict[str, Any] = {"passed": False, "certifying": False, "image_id": image}
    failure: BaseException | None = None
    result: subprocess.CompletedProcess[str] | None = None
    try:
        flags = [
            "--network=none",
            "--read-only",
            "--cpus=2",
            "--memory=1g",
            "--memory-swap=1g",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=128",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
        ]
        cid = resources.create_container(
            "probe", image, flags, command=["-W", "error", "-c", PROBE]
        )
        result = docker.run("start", "--attach", cid, timeout=timeout, check=False)
        report["attach_exit_code"] = result.returncode
        info = docker.container(cid)
        if info is None or info["State"].get("Running") is not False:
            raise InvalidExecutor("Probe container did not exit")
        report["exit_code"] = info["State"].get("ExitCode")
        if (
            type(report["exit_code"]) is not int
            or report["exit_code"] != 0
            or result.returncode != 0
        ):
            raise InvalidExecutor("Probe container execution failed")
        report["passed"] = True
    except (Exception, KeyboardInterrupt) as error:
        failure = error
        report["error_type"], report["error"] = type(error).__name__, str(error)
    finally:
        with cancellation.deferred():
            try:
                resources.close()
            except (Exception, KeyboardInterrupt) as error:
                report["cleanup_error"] = str(error)
                report["passed"] = False
                if not isinstance(failure, Cancelled):
                    failure = InvalidExecutor(str(error))
            if resources.state["containers"]:
                write_json(
                    output / "container.json",
                    {**resources.state["containers"][0], "token": resources.token},
                )
            cancellation.write_report(output / "probe-manifest.json", report)
    if failure is not None:
        raise failure
    cancellation.checkpoint()
    if result is None:
        raise InvalidExecutor("Probe returned no execution result")
    return result


def verify(args: argparse.Namespace, report: dict[str, Any]) -> None:
    root, output = args.root.resolve(), args.output
    before = source_identity(root)
    report.update(
        {
            "source_before": before,
            "source_revision": before["head"],
            "source_dirty": bool(before["status"]),
            "inputs": before["inputs"],
        }
    )
    if before["head"] != args.revision or (before["status"] and not args.development):
        raise InvalidExecutor(
            "executor source is dirty or differs from requested revision"
        )
    report["executing_before"] = executing_helpers(root, before)
    lock_text = (root / "docker/core/requirements-ci.txt").read_text()
    lock_sha = hashlib.sha256(lock_text.encode()).hexdigest()
    expected = lock_versions(lock_text)
    report["lock_sha256"] = lock_sha
    inspected = subprocess.run(
        ["docker", "image", "inspect", args.image],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env=Docker(output).environment,
    )
    (output / "inspect.json").write_text(inspected.stdout)
    images = json.loads(inspected.stdout)
    if not isinstance(images, list) or len(images) != 1:
        raise InvalidExecutor("expected exactly one executor image")
    validate_image(
        images[0],
        args.image,
        "unverified" if args.development else args.revision,
        lock_sha,
        development=args.development,
    )
    result = probe_container(args.image, output)
    (output / "stdout.log").write_text(result.stdout)
    (output / "stderr.log").write_text(result.stderr)
    report["command"], report["exit_code"] = result.args, result.returncode
    if result.returncode:
        raise InvalidExecutor(
            "executed CI environment probe failed; inspect private logs"
        )
    probe = json.loads(result.stdout)
    validate_probe(probe, expected)
    report["probe"] = probe
    report["passed"] = True


def run(args: argparse.Namespace) -> None:
    root, output = args.root.resolve(), args.output
    reserve_output(root, output)
    report: dict[str, Any] = {
        "schema_version": 1,
        "passed": False,
        "certifying": False,
        "development": args.development,
        "image_id": args.image,
        "source_unchanged": False,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    failure: BaseException | None = None
    with cancellation_scope() as cancellation:
        try:
            verify(args, report)
        except (Exception, KeyboardInterrupt) as error:
            failure = error
            report["error_type"], report["error"] = type(error).__name__, str(error)
        finally:
            with cancellation.deferred():
                if "source_before" in report:
                    try:
                        after = source_identity(root)
                        report["source_after"] = after
                        report["executing_after"] = executing_helpers(root, after)
                        if report["executing_after"] != report.get("executing_before"):
                            raise InvalidExecutor(
                                "Executing helper identity changed during the probe"
                            )
                        if after != report["source_before"]:
                            raise InvalidExecutor(
                                "Executor source changed during the probe"
                            )
                        report["source_unchanged"] = True
                    except (Exception, KeyboardInterrupt) as error:
                        report["source_error"] = str(error)
                        report["passed"] = False
                        if failure is None:
                            failure = error
                report["certifying"] = report["passed"] and not args.development
                try:
                    report["raw"] = {
                        name: digest
                        for name, digest in inventory(output).items()
                        if name != "manifest.json"
                    }
                except (OSError, ValueError) as error:
                    report["evidence_error"] = str(error)
                    report["passed"] = report["certifying"] = False
                    failure = failure or error
                cancellation.write_report(output / "manifest.json", report)
        if failure is not None:
            raise failure
        cancellation.checkpoint()
    print(
        json.dumps(
            {
                "image_id": args.image,
                "python": report["probe"]["python_version"],
                "packages": len(report["probe"]["packages"]),
                "certifying": report["certifying"],
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--development", action="store_true")
    args = parser.parse_args()
    try:
        run(args)
    except (
        Cancelled,
        InvalidExecutor,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        raise SystemExit(f"CI executor verification failed: {exc}") from None


if __name__ == "__main__":
    main()
