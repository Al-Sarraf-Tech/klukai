"""Record fail-closed component prerequisites and export only validated roles."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from . import core_source, light_environment, light_environment_sandbox
from .cancellation import cancellation_scope
from .core_source import require, sha256
from .light_environment_process import Commands

COMPOSE_VERSION = "5.6.0"
COMPOSE_SHA256 = "40343e21ca777173e69cff5dbafeb37c6f81f3b0d57d9e597f036e95eb63e76a"
COMPOSE_URL = f"https://github.com/docker/compose/releases/download/v{COMPOSE_VERSION}/docker-compose-linux-x86_64"
HELPER = "tools/ci/component_environment.py"
VERSIONS = {
    "node": ["--version"],
    "npm": ["--version"],
    "shellcheck": ["--version"],
    "docker": ["--version"],
    "curl": ["--version"],
}


def helper(root: Path, captured: dict[str, Any]) -> str:
    path = Path(__file__)
    require(
        path.is_file() and not any(p.is_symlink() for p in (path, *path.parents)),
        "Component helper is missing or linked",
    )
    digest = sha256(path)
    require(
        digest == captured["files"].get(HELPER) == sha256(root / HELPER),
        "Component helper differs from committed source",
    )
    return digest


def prerequisites(commands: Commands) -> dict[str, Any]:
    tools: dict[str, Any] = {}
    for name, arguments in VERSIONS.items():
        filename = shutil.which(name)
        require(
            filename is not None,
            f"Native {name} prerequisite missing; operator provisioning required",
        )
        path = Path(str(filename)).resolve(strict=True)
        require(
            path.is_file() and os.access(path, os.X_OK), f"Invalid {name} prerequisite"
        )
        if name == "node":
            commands.environment["PATH"] = str(path.parent) + ":/usr/bin:/bin"
        tools[name] = {
            "executable": str(path),
            "sha256": sha256(path),
            "version": commands.run([str(path), *arguments]).strip(),
        }
    require(
        tools["node"]["version"] == "v24.22.0",
        "Pinned Node24.22.0 prerequisite required",
    )
    require(bool(tools["npm"]["version"]), "npm prerequisite returned no version")
    return tools


def namespace(commands: Commands, output: Path) -> dict[str, Any]:
    if light_environment_sandbox.mode() == "unavailable":
        return dict(light_environment_sandbox.UNAVAILABLE)
    record = light_environment_sandbox.identity(commands)
    builder, downloads, wheels = (
        output / name for name in ("builder", "downloads", "wheels")
    )
    for directory in (builder, downloads, wheels):
        directory.mkdir(mode=0o700)
    lock = output / "namespace-fixture.lock"
    lock.write_text("No packages; native namespace capability probe.\n")
    command = light_environment_sandbox.command(builder, downloads, wheels, lock)
    try:
        commands.run([*command, "--", "/usr/bin/true"])
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise ValueError(
            "Native bwrap namespace prerequisite unavailable; operator provisioning required; see private logs"
        ) from error
    record["capability"] = True
    return record


def compose(commands: Commands, output: Path, tools: dict[str, Any]) -> dict[str, Any]:
    directory = output / "docker"
    directory.mkdir(mode=0o700)
    plugins = directory / "cli-plugins"
    plugins.mkdir(mode=0o700)
    path = plugins / "docker-compose"
    commands.run(
        [
            tools["curl"]["executable"],
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--max-time",
            "120",
            COMPOSE_URL,
            "--output",
            str(path),
        ]
    )
    require(
        path.is_file()
        and not path.is_symlink()
        and path.stat().st_nlink == 1
        and sha256(path) == COMPOSE_SHA256,
        "Compose checksum verification failed",
    )
    path.chmod(0o700)
    commands.environment["DOCKER_CONFIG"] = str(directory)
    version = commands.run([str(path), "version", "--short"]).strip()
    require(version == COMPOSE_VERSION, "Compose version differs from pinned renderer")
    selected = commands.run(
        [tools["docker"]["executable"], "compose", "version", "--short"]
    ).strip()
    require(
        selected == COMPOSE_VERSION,
        "Docker did not select the private pinned Compose renderer",
    )
    return {
        "executable": str(path),
        "sha256": sha256(path),
        "version": version,
        "docker_config": str(directory),
    }


def unchanged(report: dict[str, Any], output: Path) -> None:
    for name, tool in report["tools"].items():
        selected = shutil.which(name)
        require(
            selected is not None
            and str(Path(selected).resolve(strict=True)) == tool["executable"]
            and sha256(Path(tool["executable"])) == tool["sha256"],
            "Native component prerequisite changed",
        )
    light_environment_sandbox.unchanged(report["sandbox"])
    require(
        report["sandbox"]["capability"] is True
        or report["sandbox"] == light_environment_sandbox.UNAVAILABLE,
        "Missing component namespace capability",
    )
    record = report["compose"]
    path = output / "docker/cli-plugins/docker-compose"
    require(
        record
        == {
            "executable": str(path),
            "sha256": COMPOSE_SHA256,
            "version": COMPOSE_VERSION,
            "docker_config": str(output / "docker"),
        }
        and path.is_file()
        and not any(p.is_symlink() for p in (path, *path.parents))
        and path.stat().st_nlink == 1
        and os.access(path, os.X_OK)
        and sha256(path) == COMPOSE_SHA256,
        "Private Compose material changed",
    )
    require(
        set((output / "docker").iterdir()) == {output / "docker/cli-plugins"}
        and set((output / "docker/cli-plugins").iterdir()) == {path},
        "Unrelated private Docker configuration",
    )


def prepare(args: argparse.Namespace) -> None:
    root, output = args.root, args.output
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
    }
    failure: BaseException | None = None
    with cancellation_scope() as latch:
        try:
            report["source"] = core_source.snapshot(
                root, output, args.revision, development=False
            )
            require(
                report["source"]["files"] == report["source"]["worktree"]["files"],
                "Working source bytes differ from committed source",
            )
            report["helper"] = helper(root, report["source"])
            report["helpers"] = light_environment.helpers(root, report["source"])
            commands = Commands(output, args.timeout)
            report["tools"] = prerequisites(commands)
            report["sandbox"] = namespace(commands, output)
            report["compose"] = compose(commands, output, report["tools"])
            unchanged(report, output)
            report["passed"] = True
        except (Exception, KeyboardInterrupt) as error:
            failure = error
            report.update(error_type=type(error).__name__, error=str(error))
        finally:
            with latch.deferred():
                if "source" in report:
                    try:
                        core_source.unchanged(root, output, report["source"])
                        require(
                            helper(root, report["source"]) == report["helper"]
                            and light_environment.helpers(root, report["source"])
                            == report["helpers"],
                            "Component helpers changed",
                        )
                        report["source_unchanged"] = True
                    except (Exception, KeyboardInterrupt) as error:
                        report.update(passed=False, source_error=str(error))
                        failure = failure or error
                latch.write_report(output / "manifest.json", report)
        if failure is not None:
            raise failure
        latch.checkpoint()


def selection(root: Path, output: Path, revision: str) -> str:
    require(
        output.is_absolute()
        and output == output.resolve()
        and not any(p.is_symlink() for p in (output, *output.parents))
        and not output.is_relative_to(root.resolve())
        and output.stat().st_uid == os.getuid()
        and output.stat().st_mode & 0o777 == 0o700,
        "Invalid private component output",
    )
    filename = output / "manifest.json"
    require(
        filename.is_file()
        and not filename.is_symlink()
        and filename.stat().st_nlink == 1,
        "Linked/missing component manifest",
    )
    report = json.loads(filename.read_text())
    require(isinstance(report, dict), "Malformed component manifest")
    try:
        require(
            report["schema_version"] == 1
            and report["passed"] is True
            and report["source_unchanged"] is True
            and report["source_revision"] == revision
            and report["source"]["revision"] == revision,
            "Incomplete or wrong-source component prerequisites",
        )
        core_source.unchanged(root, output, report["source"])
        require(
            helper(root, report["source"]) == report["helper"]
            and light_environment.helpers(root, report["source"]) == report["helpers"],
            "Component helpers changed",
        )
        require(
            set(report["tools"]) == set(VERSIONS),
            "Incomplete component prerequisite inventory",
        )
        unchanged(report, output)
        return str(output / "docker")
    except (KeyError, TypeError) as error:
        raise ValueError("Malformed component manifest") from error


def export(args: argparse.Namespace) -> None:
    """No consumer environment file is opened before all three selections pass."""
    directory = selection(args.root, args.output, args.revision)
    tools = light_environment.selection(args.root, args.tools, args.revision)
    bridge = light_environment.selection(
        args.root, args.bridge, args.revision, role="bridge"
    )
    values = {
        "DOCKER_CONFIG": directory,
        "KLUKAI_COMPONENT_PYTHON": tools + "/bin/python",
        "KLUKAI_BRIDGE_PYTHON": bridge + "/bin/python",
    }
    require(
        all("\n" not in value and "\r" not in value for value in values.values()),
        "Invalid component export path",
    )
    with args.github_env.open("a") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in values.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "export"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--tools", type=Path)
    parser.add_argument("--bridge", type=Path)
    parser.add_argument("--github-env", type=Path)
    args = parser.parse_args()
    if args.operation == "export" and None in (
        args.tools,
        args.bridge,
        args.github_env,
    ):
        parser.error("export requires --tools, --bridge and --github-env")
    try:
        if args.operation == "prepare":
            prepare(args)
        else:
            export(args)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Component prerequisites failed: {error}") from None


if __name__ == "__main__":
    main()
