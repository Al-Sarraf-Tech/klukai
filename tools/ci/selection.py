"""Conservative push gate selection; full dispatch remains release certification."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

GROUPS = ("core", "frontend", "components")
JOBS = (
    "source-policy",
    "lint",
    "typecheck",
    "security",
    "ci-image",
    "test",
    "real-backends",
    "image-scan",
    "perf-gate",
    "frontend",
    "components",
)
GROUP_JOBS = {
    "core": {"ci-image", "test", "real-backends", "image-scan"},
    "frontend": {"frontend"},
    "components": {"components"},
}


def plan(paths: list[str] | None, *, full: bool = False) -> dict[str, bool]:
    selected: set[str] = set()
    full = full or not paths
    for name in paths or []:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or str(path) != name:
            full = True
        elif any(
            part.startswith(("requirements", "Dockerfile"))
            or part
            in {"pubspec.yaml", "pubspec.lock", "package.json", "package-lock.json"}
            for part in path.parts
        ) or name.startswith(("ops/amarillo/ci-runner/", "ops/amarillo/scripts/")):
            full = True
        elif name.startswith("docs/") or name in {
            "README.md",
            "CHANGELOG.md",
            "LICENSE",
        }:
            continue
        elif name.startswith("tgbot/"):
            continue  # retired owner channel; no active production or CI closure
        elif name.startswith(("flutter_app/", "web/")):
            selected.add("frontend")
        elif name.startswith("docker/core/") and not any(
            part.startswith(("requirements", "Dockerfile")) for part in path.parts
        ):
            selected.add("core")
        elif name.startswith(
            ("docker/voice/", "docker/events-bridge/", "ops/", "tools/avatar/")
        ):
            selected.add("components")
        else:
            full = True
    return {"full": full, **{key: full or key in selected for key in GROUPS}}


def required(selected: dict[str, bool]) -> set[str]:
    if set(selected) != {"full", *GROUPS} or any(
        type(v) is not bool for v in selected.values()
    ):
        raise ValueError("Invalid affected gate plan")
    if selected["full"] and not all(selected.values()):
        raise ValueError("Incomplete full gate plan")
    result = {"source-policy", "lint", "typecheck", "security"}
    for group, names in GROUP_JOBS.items():
        if selected[group]:
            result.update(names)
    if selected["full"]:
        result.add("perf-gate")
    return result


def check(selected: dict[str, bool], outcomes: dict) -> None:
    expected = required(selected)
    if set(outcomes) != set(JOBS):
        raise ValueError("Missing or unexpected affected CI results")
    for name, row in outcomes.items():
        result = "success" if name in expected else "skipped"
        if not isinstance(row, dict) or row.get("result") != result:
            raise ValueError(f"Gate {name} must be {result}")


def changed(before: str, after: str) -> list[str] | None:
    if not all(
        re.fullmatch(r"[0-9a-f]{40}", value) and value != "0" * 40
        for value in (before, after)
    ):
        return None
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", "-z", before, after, "--"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return (
            result.stdout.decode("utf-8", errors="strict").rstrip("\0").split("\0")
            if result.stdout
            else []
        )
    except (UnicodeError, OSError, subprocess.SubprocessError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "check"))
    parser.add_argument("--before", default="")
    parser.add_argument("--after", default="")
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    if args.operation == "check":
        check(json.loads(os.environ["CI_PLAN"]), json.loads(os.environ["NEEDS_JSON"]))
    else:
        selected = plan(changed(args.before, args.after), full=args.full)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            for key, value in selected.items():
                stream.write(f"{key}={str(value).lower()}\n")
            stream.write(
                "selected=" + json.dumps(selected, separators=(",", ":")) + "\n"
            )
        print(json.dumps(selected, sort_keys=True))


if __name__ == "__main__":
    main()
