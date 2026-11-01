#!/usr/bin/env python3
"""Run/capture frontend gates and revalidate a private, revision-bound PWA bundle.

`init` records a clean source snapshot; `run` retains exact argv and both streams;
`create` copies the once-built PWA; `verify` independently checks all retained bytes.
Tree hashes use sorted UTF-8 ``path + NUL + sha256 + newline`` records.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

FLUTTER_REVISION = "c9a6c484230f8b5e408ec57be1ef71dee1e77020"
COMMAND_FLAGS = {
    "flutter-version": ["--version", "--machine"],
    "dart-version": ["--version"],
    "chrome-version": ["--version"],
    "pub": ["pub", "get", "--enforce-lockfile"],
    "format": ["format", "--output=none", "--set-exit-if-changed", "lib", "test"],
    "analyze": ["analyze", "--no-pub", "--fatal-infos"],
    "vm": ["test", "--no-pub", "--machine", "--concurrency=4"],
    "chrome": ["test", "--no-pub", "--machine", "--platform", "chrome"],
    "build": ["--build-only"],
}
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")


class InvalidEvidence(ValueError):
    """Evidence cannot establish a successful, reproducible frontend build."""


def require(condition, message):
    if not condition:
        raise InvalidEvidence(message)


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidEvidence(f"Cannot read JSON {path}: {exc}") from exc


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def tree_digest(files):
    for name, sha in files.items():
        path = PurePosixPath(name)
        require(
            name
            and not path.is_absolute()
            and ".." not in path.parts
            and str(path) == name
            and HEX64.fullmatch(sha),
            "Invalid file inventory",
        )
    return hashlib.sha256(
        "".join(f"{name}\0{files[name]}\n" for name in sorted(files)).encode()
    ).hexdigest()


def inventory(root):
    require(root.is_dir() and not root.is_symlink(), f"Missing/linked tree: {root}")
    files = {}
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), f"Symlink in evidence tree: {path}")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = digest(path)
        else:
            require(path.is_dir(), f"Special file in evidence tree: {path}")
    require(files, f"Empty evidence tree: {root}")
    return {"files": files, "sha256": tree_digest(files)}


def contained(root, name):
    path = root / name
    require(
        isinstance(name, str) and not Path(name).is_absolute(), "Absolute evidence path"
    )
    require(".." not in Path(name).parts, "Evidence path traversal")
    require(
        path.is_relative_to(root)
        and not any(p.is_symlink() for p in (path, *path.parents)),
        f"Linked evidence path: {name}",
    )
    return path


def private_output(output):
    require(
        output.is_absolute() and output.is_dir() and not output.is_symlink(),
        "Output must be an absolute private directory",
    )
    require(
        output.stat().st_uid == os.getuid() and output.stat().st_mode & 0o077 == 0,
        "Output directory must be owner-only (0700)",
    )
    require(not any(p.is_symlink() for p in output.parents), "Linked output parent")


def file_record(root, path):
    contained(root, path.relative_to(root).as_posix())
    return {"path": path.relative_to(root).as_posix(), "sha256": digest(path)}


def parse_machine(path, platform, exit_code):
    require(type(exit_code) is int and exit_code == 0, "Test command failed")
    require(platform in ("vm", "chrome"), "Unsupported test platform")
    started, finished, suites = set(), set(), set()
    counts = {"passed": 0, "skipped": 0, "hidden": 0, "terminal_success": False}
    event_count = 0
    allowed = {
        "start",
        "suite",
        "testStart",
        "testDone",
        "group",
        "print",
        "allSuites",
        "done",
    }
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            # Flutter's VM runner inserts blank separators; these are not events.
            if not line.strip():
                continue
            event = json.loads(line)
            require(not counts["terminal_success"], "Event after terminal done")
            # Flutter 3.44.2 event_printer.dart multiplexes this exact VM tool
            # notification with package:test events. Never discard arbitrary
            # JSON arrays (which could conceal errors or malformed events).
            if isinstance(event, list):
                require(
                    platform == "vm"
                    and event_count > 0
                    and event
                    == [
                        {
                            "event": "test.startedProcess",
                            "params": {"vmServiceUri": None},
                        }
                    ],
                    "Unknown Flutter tool envelope",
                )
                continue
            require(isinstance(event, dict), "Machine event must be an object")
            kind = event.get("type")
            require(kind in allowed, f"Error or unknown machine event: {kind}")
            require(
                (event_count == 0) == (kind == "start"),
                "Missing/duplicate protocol start",
            )
            event_count += 1
            if kind == "suite":
                suite = event["suite"]
                require(
                    suite["platform"] == platform and suite["id"] not in suites,
                    "Wrong/duplicate suite platform",
                )
                suites.add(suite["id"])
            elif kind == "testStart":
                test = event["test"]
                require(
                    test["id"] not in started and test["suiteID"] in suites,
                    "Unknown suite or duplicate test",
                )
                started.add(test["id"])
            elif kind == "testDone":
                test_id = event["testID"]
                require(
                    test_id in started and test_id not in finished,
                    "Unknown/duplicate testDone",
                )
                require(event["result"] == "success", "Unsuccessful testDone")
                require(
                    type(event["skipped"]) is bool and type(event["hidden"]) is bool,
                    "Missing/invalid testDone flags",
                )
                finished.add(test_id)
                key = (
                    "hidden"
                    if event["hidden"]
                    else ("skipped" if event["skipped"] else "passed")
                )
                counts[key] += 1
            elif kind == "done":
                require(event.get("success") is True, "Unsuccessful terminal event")
                require(
                    started == finished and counts["passed"] > 0,
                    "Unfinished or zero executed tests",
                )
                counts["terminal_success"] = True
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise InvalidEvidence(f"Invalid machine stream {path}: {exc}") from exc
    require(
        event_count > 0 and counts["terminal_success"],
        "Missing successful terminal event",
    )
    return counts


def source_snapshot(root, revision):
    require(HEX40.fullmatch(revision), "Expected a full source revision")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args]).decode().strip()

    require(
        git("rev-parse", "HEAD") == revision,
        "Source revision differs from requested revision",
    )
    require(
        not git("status", "--porcelain", "--untracked-files=all"),
        "Source checkout must be clean",
    )
    names = git(
        "ls-files",
        "flutter_app",
        "scripts/ci/frontend.sh",
        "scripts/deploy-web.sh",
        "tools/ci/frontend_evidence.py",
        "tools/ci/font_cache.py",
        "tools/ci/fallback-fonts.lock.json",
    ).splitlines()
    files = {name: digest(root / name) for name in names}
    require(
        files and "flutter_app/pubspec.lock" in files, "Missing locked frontend sources"
    )
    return {
        "revision": revision,
        "git_tree": git("rev-parse", "HEAD^{tree}"),
        "inputs": {"files": files, "sha256": tree_digest(files)},
    }


def initialize(root, output, revision):
    require(
        output.is_absolute()
        and output == output.resolve()
        and not output.is_relative_to(root.resolve()),
        "Output must be absolute and outside the checkout",
    )
    require(
        not output.exists() and not output.is_symlink(),
        "Output must be a new run directory",
    )
    require(not any(p.is_symlink() for p in output.parents), "Linked output parent")
    source = source_snapshot(root, revision)
    output.mkdir(mode=0o700, parents=True)
    private_output(output)
    (output / "raw").mkdir(mode=0o700)
    write_json(output / "source.json", source)


def run_command(output, name, cwd, argv):
    private_output(output)
    require(name in COMMAND_FLAGS and argv, "Unknown command/empty argv")
    require(not (output / "raw" / f"{name}.json").exists(), "Command already executed")
    stdout, stderr = (
        output / "raw" / f"{name}.{suffix}" for suffix in ("stdout", "stderr")
    )
    started = datetime.now(timezone.utc).isoformat()
    with stdout.open("wb") as out, stderr.open("wb") as err:
        try:
            code = subprocess.run(
                argv, cwd=cwd, stdout=out, stderr=err, check=False
            ).returncode
        except OSError as exc:
            err.write(str(exc).encode())
            code = 127
    record = {
        "name": name,
        "argv": argv,
        "cwd": str(cwd),
        "exit_code": code,
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "stdout": file_record(output, stdout),
        "stderr": file_record(output, stderr),
    }
    write_json(output / "raw" / f"{name}.json", record)
    print(f"{name}: exit {code}; raw output in {stdout}")
    return code if code >= 0 else 128 - code


def validate_commands(output, commands):
    require(
        isinstance(commands, list) and len(commands) == len(COMMAND_FLAGS),
        "Missing commands",
    )
    require(
        [c["name"] for c in commands] == list(COMMAND_FLAGS),
        "Missing/duplicate/out-of-order commands",
    )
    texts = {}
    for command in commands:
        name = command["name"]
        require(
            type(command["exit_code"]) is int and command["exit_code"] == 0,
            f"Failed command: {name}",
        )
        argv = command["argv"]
        require(
            isinstance(argv, list) and all(isinstance(a, str) for a in argv),
            "Invalid argv",
        )
        require(
            all(flag in argv for flag in COMMAND_FLAGS[name]),
            f"Missing required flag: {name}",
        )
        if name == "vm":
            require(
                not any(
                    arg == "--platform" or arg.startswith("--platform=") for arg in argv
                ),
                "VM tests must use Flutter's default tester platform",
            )
        elif name == "chrome":
            require(
                argv.count("--platform") == 1
                and argv[argv.index("--platform") + 1] == "chrome",
                "Wrong command platform",
            )
        for stream in ("stdout", "stderr"):
            record = command[stream]
            path = contained(output, record["path"])
            require(
                path.is_file() and digest(path) == record["sha256"],
                "Missing/altered raw output",
            )
        texts[name] = contained(output, command["stdout"]["path"]).read_text()
    validate_versions(texts)


def validate_versions(texts):
    version = json.loads(texts["flutter-version"])
    require(
        version.get("frameworkVersion") == "3.44.2"
        and version.get("dartSdkVersion") == "3.12.2"
        and version.get("frameworkRevision") == FLUTTER_REVISION,
        "Wrong pinned Flutter/Dart identity",
    )
    require(
        re.match(r"Dart SDK version: 3\.12\.2(?:\s|$)", texts["dart-version"]),
        "Wrong Dart identity",
    )
    require(
        re.match(
            r"(?:Google Chrome|Chromium) \d+\.\d+\.\d+\.\d+\s*$",
            texts["chrome-version"],
        ),
        "Missing Chrome identity",
    )


def capture_tools(output, executables):
    texts = {}
    for name in ("flutter", "dart", "chrome"):
        record = load_json(output / "raw" / f"{name}-version.json")
        require(
            type(record["exit_code"]) is int and record["exit_code"] == 0,
            "Tool identity command failed",
        )
        texts[f"{name}-version"] = contained(
            output, record["stdout"]["path"]
        ).read_text()
    validate_versions(texts)
    tools = tool_records(output, executables)
    write_json(output / "tools.json", tools)
    return tools


def tool_records(output, executables):
    tools = {
        name: {
            "path": str(path.resolve()),
            "sha256": digest(path.resolve()),
            "identity": (
                load_json(output / "raw/flutter-version.stdout")
                if name == "flutter"
                else (output / "raw" / f"{name}-version.stdout").read_text().strip()
            ),
        }
        for name, path in executables.items()
    }
    return tools


def validate_bundle(output, revision):
    try:
        private_output(output)
        data = load_json(contained(output, "manifest.json"))
        require(
            type(data["schema_version"]) is int and data["schema_version"] == 1,
            "Unknown manifest schema",
        )
        source = data["source"]
        require(
            HEX40.fullmatch(revision)
            and source["revision"] == revision
            and HEX40.fullmatch(source["git_tree"]),
            "Source revision mismatch",
        )
        inputs = source["inputs"]
        require(
            inputs["files"] and tree_digest(inputs["files"]) == inputs["sha256"],
            "Source input hash mismatch",
        )
        validate_commands(output, data["commands"])
        require(
            set(data["tools"]) == {"flutter", "dart", "chrome"},
            "Missing tool identities",
        )
        for tool in data["tools"].values():
            require(
                Path(tool["path"]).is_absolute() and HEX64.fullmatch(tool["sha256"]),
                "Invalid tool identity",
            )
        for name, tool in data["tools"].items():
            path = output / "raw" / f"{name}-version.stdout"
            identity = (
                load_json(path) if name == "flutter" else path.read_text().strip()
            )
            require(
                tool["identity"] == identity, "Tool identity differs from raw output"
            )
        provenance = data["sdk_provenance"]
        kind, sha = provenance["kind"], provenance["archive_sha256"]
        require(
            kind in {"installed", "owner-snapshot", "hash-pinned-https-archive"}
            and ((sha is None) if kind == "installed" else HEX64.fullmatch(sha)),
            "Missing SDK archive provenance",
        )
        for platform in ("vm", "chrome"):
            command = next(c for c in data["commands"] if c["name"] == platform)
            actual = parse_machine(
                contained(output, command["stdout"]["path"]),
                platform,
                command["exit_code"],
            )
            require(
                actual == data["suites"][platform],
                "Suite summary differs from raw events",
            )
        require(
            tree_digest(data["pwa"]["files"]) == data["pwa"]["sha256"],
            "Invalid PWA tree hash",
        )
        require(
            inventory(output / "pwa") == data["pwa"],
            "PWA differs from retained inventory",
        )
        require(
            {"index.html", "main.dart.js", "flutter_bootstrap.js", "login.html"}
            <= data["pwa"]["files"].keys(),
            "Incomplete PWA",
        )
        return data
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration) as exc:
        raise InvalidEvidence(f"Invalid frontend bundle: {exc}") from exc


def create_manifest(root, output, revision, executables):
    private_output(output)
    source = source_snapshot(root, revision)
    require(
        source == load_json(output / "source.json"),
        "Source changed during frontend gates",
    )
    commands = [load_json(output / "raw" / f"{name}.json") for name in COMMAND_FLAGS]
    validate_commands(output, commands)
    built = root / "flutter_app/build/web"
    built_inventory = inventory(built)
    require(not (output / "pwa").exists(), "Artifact already captured")
    shutil.copytree(built, output / "pwa")
    tools = tool_records(output, executables)
    require(
        tools == load_json(output / "tools.json"),
        "Tool executable changed during frontend gates",
    )
    manifest = {
        "schema_version": 1,
        "source": source,
        "tools": tools,
        "sdk_provenance": load_json(output / "sdk-provenance.json"),
        "commands": commands,
        "pwa": built_inventory,
        "suites": {
            platform: parse_machine(output / "raw" / f"{platform}.stdout", platform, 0)
            for platform in ("vm", "chrome")
        },
    }
    write_json(output / "manifest.json", manifest)
    return validate_bundle(output, revision)


def select_tests(root, platform):
    files = []
    for path in sorted((root / "flutter_app/test").rglob("*_test.dart")):
        browser = path.read_text().startswith("@TestOn('browser')")
        if browser == (platform == "chrome"):
            files.append(str(path.relative_to(root / "flutter_app")))
    require(files, f"No tests for {platform}")
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("init", "create", "verify", "run", "tools"):
        command = sub.add_parser(action)
        command.add_argument("--output", type=Path, required=True)
        if action in ("init", "create"):
            command.add_argument("--root", type=Path, required=True)
        if action not in ("run", "tools"):
            command.add_argument("--revision", required=True)
        if action in ("create", "tools"):
            for name in ("flutter", "dart", "chrome"):
                command.add_argument(f"--{name}", type=Path, required=True)
        if action == "run":
            command.add_argument("--name", required=True)
            command.add_argument("--cwd", type=Path, required=True)
            command.add_argument("argv", nargs=argparse.REMAINDER)
    machine = sub.add_parser("machine")
    machine.add_argument("path", type=Path)
    machine.add_argument("--platform", choices=("vm", "chrome"), required=True)
    machine.add_argument("--exit-code", type=int, required=True)
    select = sub.add_parser("select-tests")
    select.add_argument("--root", type=Path, required=True)
    select.add_argument("--platform", choices=("vm", "chrome"), required=True)
    args = parser.parse_args()
    try:
        if args.action == "init":
            initialize(args.root, args.output, args.revision)
        elif args.action == "run":
            return run_command(
                args.output,
                args.name,
                args.cwd,
                args.argv[1:] if args.argv[:1] == ["--"] else args.argv,
            )
        elif args.action == "machine":
            print(json.dumps(parse_machine(args.path, args.platform, args.exit_code)))
        elif args.action == "select-tests":
            sys.stdout.write(
                "".join(name + "\0" for name in select_tests(args.root, args.platform))
            )
        elif args.action == "tools":
            capture_tools(
                args.output,
                {name: getattr(args, name) for name in ("flutter", "dart", "chrome")},
            )
        else:
            data = (
                create_manifest(
                    args.root,
                    args.output,
                    args.revision,
                    {
                        name: getattr(args, name)
                        for name in ("flutter", "dart", "chrome")
                    },
                )
                if args.action == "create"
                else validate_bundle(args.output, args.revision)
            )
            print(
                json.dumps(
                    {
                        "revision": data["source"]["revision"],
                        "pwa_sha256": data["pwa"]["sha256"],
                        "suites": data["suites"],
                    }
                )
            )
        return 0
    except (InvalidEvidence, OSError, subprocess.SubprocessError) as exc:
        print(f"frontend evidence: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
