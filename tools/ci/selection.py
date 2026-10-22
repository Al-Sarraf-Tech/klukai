"""Conservative push gate selection; full dispatch remains release certification."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import subprocess
import urllib.request
from collections.abc import Callable
from pathlib import Path, PurePosixPath

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
FULL_CI_GATES = {
    "Authorized exact source",
    "Pinned Python CI executor",
    "Isolated component suites",
    "Test Suite",
    "Real PostgreSQL, Valkey and Qdrant recovery",
    "Lint",
    "Type Check (mypy)",
    "Security Scan (bandit + safety)",
    "Image Scan (trivy)",
    "Perf Gate (p99 regression check)",
    "Frontend validation and immutable PWA evidence",
    "Required CI gates",
    "Affected CI gates",
}
BASELINE_RUN_LIMIT = 20


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def github(path: str, token: str) -> dict:
    request = urllib.request.Request(
        "https://api.github.com" + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    # Never forward the token on redirects; limit both wait and response size.
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=5) as response:
        payload = response.read(2 * 1024 * 1024 + 1)
    if len(payload) > 2 * 1024 * 1024:
        raise ValueError("Oversized CI baseline response")
    return json.loads(payload)


def full_success_baseline(
    *,
    repository: str,
    ref: str,
    after: str,
    current_run: str,
    token: str,
    api: Callable[[str, str], dict] = github,
) -> str | None:
    """Find a certified ancestor, never treating a routine success as coverage.

    Lookup is bounded to twenty recent successful runs. Missing evidence,
    unavailable history and API errors select full CI instead of event.before.
    """
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or ref != "refs/heads/main"
        or not re.fullmatch(r"[0-9a-f]{40}", after)
        or after == "0" * 40
        or not re.fullmatch(r"[1-9][0-9]*", current_run)
        or not token
    ):
        return None
    try:
        runs = api(
            f"/repos/{repository}/actions/workflows/ci.yml/runs"
            f"?branch=main&status=success&per_page={BASELINE_RUN_LIMIT}",
            token,
        )["workflow_runs"]
        if not isinstance(runs, list):
            return None
        for run in runs[:BASELINE_RUN_LIMIT]:
            if not isinstance(run, dict):
                return None
            run_id, attempt = run.get("id"), run.get("run_attempt")
            revision = run.get("head_sha")
            if (
                type(run_id) is not int
                or run_id <= 0
                or str(run_id) == current_run
                or type(attempt) is not int
                or attempt <= 0
                or run.get("path") != ".github/workflows/ci.yml"
                or run.get("event") not in {"push", "workflow_dispatch"}
                or run.get("status") != "completed"
                or run.get("conclusion") != "success"
                or run.get("head_branch") != "main"
                or not isinstance(run.get("head_repository"), dict)
                or run["head_repository"].get("full_name") != repository
                or not isinstance(revision, str)
                or not re.fullmatch(r"[0-9a-f]{40}", revision)
            ):
                continue
            ancestor = subprocess.run(
                ["git", "merge-base", "--is-ancestor", revision, after],
                capture_output=True,
                timeout=5,
            )
            if ancestor.returncode == 1:
                continue
            if ancestor.returncode != 0:
                return None
            evidence = api(
                f"/repos/{repository}/actions/runs/{run_id}/attempts/{attempt}"
                "/jobs?per_page=100",
                token,
            )
            jobs = evidence["jobs"]
            if (
                not isinstance(jobs, list)
                or evidence.get("total_count") != len(jobs)
                or len(jobs) != len(FULL_CI_GATES)
                or not all(isinstance(job, dict) for job in jobs)
                or {job.get("name") for job in jobs} != FULL_CI_GATES
            ):
                continue
            if all(
                job.get("status") == "completed"
                and job.get("conclusion") == "success"
                and job.get("run_id") == run_id
                and job.get("head_sha") == revision
                and job.get("run_attempt", attempt) == attempt
                for job in jobs
            ):
                return revision
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        http.client.HTTPException,
        subprocess.SubprocessError,
    ):
        # Do not log API errors or request objects: they can contain credentials.
        return None
    return None


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
        elif name.startswith("flutter_app/web/"):
            # Component Node tests also exercise web HTML, CSS and JS bridges.
            selected.update(("frontend", "components"))
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
    parser.add_argument("--cumulative", action="store_true")
    args = parser.parse_args()
    if args.operation == "check":
        check(json.loads(os.environ["CI_PLAN"]), json.loads(os.environ["NEEDS_JSON"]))
    else:
        before = args.before
        if args.cumulative and not args.full:
            before = (
                full_success_baseline(
                    repository=os.environ.get("GITHUB_REPOSITORY", ""),
                    ref=os.environ.get("GITHUB_REF", ""),
                    after=args.after,
                    current_run=os.environ.get("GITHUB_RUN_ID", ""),
                    token=os.environ.get("GH_TOKEN", ""),
                )
                or ""
            )
        selected = plan(changed(before, args.after), full=args.full)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            for key, value in selected.items():
                stream.write(f"{key}={str(value).lower()}\n")
            stream.write(
                "selected=" + json.dumps(selected, separators=(",", ":")) + "\n"
            )
        print(json.dumps(selected, sort_keys=True))


if __name__ == "__main__":
    main()
