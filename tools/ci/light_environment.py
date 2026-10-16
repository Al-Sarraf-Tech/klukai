"""Build private complete light-job tools with the trusted native CPython."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
from typing import Any
from zipfile import BadZipFile

from . import (
    cancellation,
    core_resources,
    core_source,
    python_executor,
    tool_materials,
    light_environment_sandbox,
    light_environment_installed,
    light_environment_process,
    light_environment_supervisor,
    light_environment_ca,
    light_environment_role,
    light_environment_init,
)
from .cancellation import cancellation_scope
from .light_environment_process import Commands
from .core_source import require, sha256

INPUTS = (
    "docker/core/requirements-build.txt",
    "docker/core/requirements-ci.txt",
    "docker/core/package_runtime.py",
    "tools/ci/light_environment.py",
    "tools/ci/light_environment_sandbox.py",
    "tools/ci/light_environment_installed.py",
    "tools/ci/light_environment_process.py",
    "tools/ci/light_environment_supervisor.py",
    "tools/ci/light_environment_ca.py",
    "tools/ci/light_environment_role.py",
    "tools/ci/light_environment_init.py",
    "tools/ci/core_source.py",
    "tools/ci/cancellation.py",
    "tools/ci/tool_materials.py",
    "tools/ci/python_executor.py",
    "tools/ci/core_resources.py",
)
HOST_PROBE = """import json, platform, ssl, sys
print(json.dumps({'implementation':platform.python_implementation(),
'version':platform.python_version(),'version_info':list(sys.version_info[:3]),
'platform':platform.platform(),'openssl':ssl.OPENSSL_VERSION,
'machine':platform.machine(),'executable':sys.executable}))
"""
PACKAGE_PROBE = """import importlib.metadata, json, os, sys
roots=list(dict.fromkeys(os.path.realpath(path) for path in sys.path))
print(json.dumps({'packages':[[d.metadata['Name'],d.version]
for d in importlib.metadata.distributions(path=roots)]}))
"""


def packages(value: Any, expected: dict[str, str]) -> dict[str, str]:
    require(
        isinstance(value, dict) and isinstance(value.get("packages"), list),
        "Malformed installed package probe",
    )
    result: dict[str, str] = {}
    for row in value["packages"]:
        require(
            isinstance(row, list)
            and len(row) == 2
            and all(isinstance(item, str) and item for item in row),
            "Malformed installed distribution",
        )
        name = re.sub(r"[-_.]+", "-", row[0]).lower()
        require(name not in result, "Duplicate installed distribution")
        result[name] = row[1]
    require(
        result == expected, "Installed packages differ from exclusive complete lock"
    )
    return result


def helpers(root: Path, captured: dict[str, Any]) -> dict[str, Any]:
    locations = {
        "tools/ci/light_environment.py": __file__,
        "tools/ci/light_environment_sandbox.py": light_environment_sandbox.__file__,
        "tools/ci/light_environment_installed.py": light_environment_installed.__file__,
        "tools/ci/light_environment_process.py": light_environment_process.__file__,
        "tools/ci/light_environment_supervisor.py": light_environment_supervisor.__file__,
        "tools/ci/light_environment_ca.py": light_environment_ca.__file__,
        "tools/ci/light_environment_role.py": light_environment_role.__file__,
        "tools/ci/light_environment_init.py": light_environment_init.__file__,
        "tools/ci/core_source.py": core_source.__file__,
        "tools/ci/cancellation.py": cancellation.__file__,
        "tools/ci/tool_materials.py": tool_materials.__file__,
        "tools/ci/python_executor.py": python_executor.__file__,
        "tools/ci/core_resources.py": core_resources.__file__,
    }
    result = {}
    for name, filename in locations.items():
        require(isinstance(filename, str), "Helper has no location")
        path = Path(filename)
        require(
            path.is_file() and not any(p.is_symlink() for p in (path, *path.parents)),
            "Executing helper is missing or linked",
        )
        digest = sha256(path)
        require(
            digest == captured["files"].get(name) == sha256(root / name),
            "Executing helper differs from committed selected source",
        )
        result[name] = {"path": str(path.absolute()), "sha256": digest}
    return result


def wheel_materials(output: Path, expected: dict[str, str]) -> dict[str, Any]:
    filename = output / "source/docker/core/package_runtime.py"
    wheel_lock = runpy.run_path(str(filename))["wheel_lock"]
    try:
        wheel_lock(
            output / "wheels", output / "runtime.lock", output / "wheel-materials.json"
        )
    except BadZipFile as error:
        raise ValueError("Corrupt wheel material") from error
    manifest: dict[str, Any] = json.loads((output / "wheel-materials.json").read_text())
    packages(
        {"packages": [[r["name"], r["version"]] for r in manifest["wheels"]]}, expected
    )
    return manifest


def bootstrap(args: argparse.Namespace, report: dict[str, Any]) -> None:
    root, output = args.root, args.output
    captured = core_source.snapshot(root, output, args.revision, development=False)
    report["source"] = captured
    require(
        captured["files"] == captured["worktree"]["files"],
        "Working source bytes differ from committed source",
    )
    require(
        all(name in captured["files"] for name in INPUTS), "Missing bootstrap input"
    )
    report["helpers"] = helpers(root, captured)
    report["inputs"] = {name: captured["files"][name] for name in INPUTS}
    role = getattr(args, "role", "tools")
    selected_inputs = (*INPUTS, *light_environment_role.inputs(role))
    require(
        all(name in captured["files"] for name in selected_inputs), "Missing role input"
    )
    report["inputs"] = {name: captured["files"][name] for name in selected_inputs}
    report["role"], report["lock"] = role, light_environment_role.lock(role)
    build_lock = output / "source/docker/core/requirements-build.txt"
    ci_lock = output / "source" / report["lock"]
    tool_materials.validate_hash_lock(build_lock.read_text(), 3)
    expected = light_environment_role.expected(output / "source", role)
    commands = Commands(output, args.timeout)
    host = Path(sys.executable).resolve(strict=True)
    host_hash = sha256(host)
    probe = json.loads(commands.run([str(host), "-I", "-c", HOST_PROBE]))
    require(
        isinstance(probe, dict)
        and probe.get("implementation") == "CPython"
        and isinstance(probe.get("version_info"), list)
        and len(probe["version_info"]) == 3
        and probe["version_info"][0] == 3
        and probe["version_info"][1] in {12, 13, 14},
        "Native prerequisite must be CPython 3.12 through 3.14",
    )
    report["host"] = {**probe, "executable": str(host), "sha256": host_hash}
    builder, environment = output / "builder", output / "environment"
    downloads, wheels = output / "downloads", output / "wheels"
    downloads.mkdir(mode=0o700)
    wheels.mkdir(mode=0o700)
    commands.run([str(host), "-I", "-m", "venv", str(builder)])
    python = str(builder / "bin/python")
    pip = [python, "-I", "-m", "pip", "--isolated"]
    report["bootstrap_pip"] = commands.run([*pip, "--version"]).strip()
    pinned = ["--no-deps", "--require-hashes", "--no-build-isolation"]
    public = ["--index-url=https://pypi.org/simple", "--no-input", "--no-cache-dir"]
    commands.run([*pip, "install", *public, *pinned, "-r", str(build_lock)])
    report["build_tools"] = commands.run([python, "-I", "-c", PACKAGE_PROBE])
    commands.run(
        [
            *pip,
            "download",
            *public,
            *pinned,
            "--dest",
            str(downloads),
            "-r",
            str(ci_lock),
        ]
    )
    report["downloads"] = core_source.inventory(downloads)
    report["builder_ca"] = light_environment_ca.discover(commands, builder)
    report["sandbox"] = light_environment_sandbox.identity(commands)
    sandbox = light_environment_sandbox.command(
        builder, downloads, wheels, ci_lock, ca=report["builder_ca"]
    )
    commands.run([*sandbox, "--", "/usr/bin/true"])
    report["sandbox"]["capability"] = True
    commands.run(
        [
            *sandbox,
            "--",
            *pip,
            "wheel",
            "--no-index",
            "--find-links",
            str(downloads),
            *pinned,
            "-r",
            str(ci_lock),
            "--wheel-dir",
            str(wheels),
        ]
    )
    report["wheel_materials"] = wheel_materials(output, expected)
    report["runtime_lock_sha256"] = sha256(output / "runtime.lock")
    commands.run([str(host), "-I", "-m", "venv", "--without-pip", str(environment)])
    final_python = str(environment / "bin/python")
    commands.run(
        [
            *pip,
            "--python",
            final_python,
            "install",
            "--no-index",
            "--find-links",
            str(wheels),
            "--no-deps",
            "--require-hashes",
            "--no-compile",
            "--no-input",
            "-r",
            str(output / "runtime.lock"),
        ]
    )
    report["packages"] = packages(
        json.loads(commands.run([final_python, "-I", "-c", PACKAGE_PROBE])), expected
    )
    commands.run([final_python, "-I", "-m", "pip", "--isolated", "check"])
    require(sha256(host) == host_hash, "Native interpreter changed during bootstrap")
    light_environment_sandbox.unchanged(report["sandbox"])
    light_environment_ca.unchanged(report["builder_ca"], builder)
    report["installed_environment"] = light_environment_installed.capture(
        environment, host
    )
    report["environment"] = str(environment)


def run(args: argparse.Namespace) -> None:
    root, output = args.root, args.output
    light_environment_role.lock(getattr(args, "role", "tools"))
    require(
        root.is_absolute()
        and root == root.resolve()
        and not any(p.is_symlink() for p in (root, *root.parents)),
        "Source must be absolute and nonlinked",
    )
    core_source.reserve_output(root, output)
    report: dict[str, Any] = {
        "schema_version": 1,
        "passed": False,
        "source_unchanged": False,
        "source_revision": args.revision,
        "runtime_equivalent": False,
    }
    failure: BaseException | None = None
    with cancellation_scope() as latch:
        try:
            bootstrap(args, report)
            report["passed"] = True
        except (Exception, KeyboardInterrupt) as error:
            failure = error
            report["error_type"], report["error"] = type(error).__name__, str(error)
        finally:
            with latch.deferred():
                if "source" in report:
                    try:
                        core_source.unchanged(root, output, report["source"])
                        if "helpers" in report:
                            require(
                                helpers(root, report["source"]) == report["helpers"],
                                "Executing helper changed during bootstrap",
                            )
                        report["source_unchanged"] = True
                    except (Exception, KeyboardInterrupt) as error:
                        report["passed"] = False
                        report["source_error"] = str(error)
                        report.setdefault("error", str(error))
                        failure = failure or error
                latch.write_report(output / "manifest.json", report)
        if failure is not None:
            raise failure
        latch.checkpoint()


def selection(root: Path, output: Path, revision: str, *, role: str = "tools") -> str:
    """Revalidate source and private terminal evidence before GitHub exports."""
    require(
        output.is_absolute()
        and output == output.resolve()
        and not output.is_relative_to(root.resolve())
        and output.stat().st_uid == os.getuid()
        and output.stat().st_mode & 0o777 == 0o700,
        "Invalid private bootstrap output",
    )
    light_environment_role.lock(role)
    filename = output / "manifest.json"
    require(
        filename.is_file()
        and not filename.is_symlink()
        and filename.stat().st_nlink == 1,
        "Linked/missing terminal manifest",
    )
    report = json.loads(filename.read_text())
    require(isinstance(report, dict), "Malformed terminal manifest")
    try:
        require(
            report["schema_version"] == 1
            and report["passed"] is True
            and report["source_unchanged"] is True
            and report["source_revision"] == revision
            and report["source"]["revision"] == revision,
            "Incomplete, failed or wrong-source native bootstrap",
        )
        require(
            (report["role"], report["lock"])
            == (role, light_environment_role.lock(role)),
            "Native environment role/lock mismatch",
        )
        expected = light_environment_role.expected(root, role)
        require(report["packages"] == expected, "Incomplete native tools manifest")
        core_source.unchanged(root, output, report["source"])
        require(
            helpers(root, report["source"]) == report["helpers"],
            "Bootstrap helpers changed",
        )
        require(
            report["inputs"]
            == {
                name: report["source"]["files"][name]
                for name in (*INPUTS, *light_environment_role.inputs(role))
            },
            "Bootstrap input identities differ",
        )
        require(
            sha256(Path(report["host"]["executable"])) == report["host"]["sha256"],
            "Native interpreter changed after bootstrap",
        )
        light_environment_sandbox.unchanged(report["sandbox"])
        require(
            report["sandbox"]["capability"] is True,
            "Missing offline sandbox capability",
        )
        require(
            sha256(output / "runtime.lock") == report["runtime_lock_sha256"],
            "Wheel lock changed",
        )
        material = json.loads((output / "wheel-materials.json").read_text())
        require(
            material == report["wheel_materials"], "Wheel material manifest changed"
        )
        for wheel in material["wheels"]:
            path = output / "wheels" / wheel["file"]
            require(
                path.parent == output / "wheels"
                and not path.is_symlink()
                and sha256(path) == wheel["sha256"],
                "Wheel material changed",
            )
        environment = str(output / "environment")
        require(
            report["environment"] == environment
            and "\n" not in environment
            and "\r" not in environment
            and Path(environment).is_dir()
            and Path(environment) == Path(environment).resolve(),
            "Invalid native environment path",
        )
    except (KeyError, TypeError) as error:
        raise ValueError("Malformed native tools manifest") from error
    light_environment_ca.unchanged(report.get("builder_ca"), output / "builder")
    light_environment_installed.unchanged(
        Path(environment),
        Path(report["host"]["executable"]),
        report.get("installed_environment"),
    )
    return environment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--role", choices=("tools", "bridge"), default="tools")
    args = parser.parse_args()
    try:
        run(args)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Light Python bootstrap failed: {error}") from None


if __name__ == "__main__":
    main()
