"""Affected gates are conservative and never certify a partial release."""

import copy
import http.client
import json
import subprocess
import sys

import pytest

from tools.ci import selection


@pytest.mark.parametrize(
    "paths,expected",
    [
        (["README.md"], set()),
        (["docs/handoff/example.md"], set()),
        (["flutter_app/lib/chat.dart"], {"frontend"}),
        (["web/index.html"], {"frontend"}),
        (["docker/core/app/chat.py"], {"core"}),
        (["docker/core/tests/test_chat.py"], {"core"}),
        (["ops/amarillo/tests/test_ci_runner.py"], {"components"}),
        (["docker/voice/app.py"], {"components"}),
        (["tools/avatar/test.mjs"], {"components"}),
        (["tgbot/bot.py"], set()),
        (
            ["docker/core/app/chat.py", "flutter_app/lib/chat.dart"],
            {"core", "frontend"},
        ),
    ],
)
def test_known_components_select_only_affected_gates(paths, expected):
    result = selection.plan(paths)
    assert result == {
        "full": False,
        **{key: key in expected for key in selection.GROUPS},
    }


@pytest.mark.parametrize(
    "path",
    [
        "flutter_app/web/companion/voice.js",
        "flutter_app/web/companion.html",
        "flutter_app/web/companion/companion.css",
        "flutter_app/web/js/audio_recorder.js",
        "flutter_app/web/js/pretext_bridge.js",
    ],
)
def test_flutter_web_assets_require_frontend_and_component_regressions(path):
    selected = selection.plan([path])
    assert selected == {
        "full": False,
        "core": False,
        "frontend": True,
        "components": True,
    }
    assert selection.required(selected) == {
        "source-policy",
        "lint",
        "typecheck",
        "security",
        "frontend",
        "components",
    }


@pytest.mark.parametrize(
    "path",
    [
        "unknown.py",
        ".github/workflows/ci.yml",
        "Makefile",
        "tools/ci/core_runner.py",
        "docker/core/requirements-ci.txt",
        "docker/core/Dockerfile",
        "scripts/ci/frontend.sh",
        "tests/quality/test_ci_selection.py",
        "../bad",
        "/absolute",
    ],
)
def test_unknown_shared_build_or_dependency_changes_run_every_gate(path):
    assert all(selection.plan([path]).values())


def test_dispatch_and_unavailable_diff_always_run_full():
    assert all(selection.plan(["README.md"], full=True).values())
    assert all(selection.plan(None).values())
    assert all(selection.plan([]).values())


def test_routine_result_requires_each_planned_gate_and_rejects_unplanned_execution():
    selected = selection.plan(["flutter_app/lib/chat.dart"])
    outcomes = {
        name: {
            "result": "success" if name in selection.required(selected) else "skipped"
        }
        for name in selection.JOBS
    }
    selection.check(selected, outcomes)
    outcomes["frontend"]["result"] = "failure"
    with pytest.raises(ValueError):
        selection.check(selected, outcomes)
    outcomes["frontend"]["result"] = "skipped"
    with pytest.raises(ValueError):
        selection.check(selected, outcomes)


FULL_JOB_NAMES = {
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


@pytest.fixture
def push_history(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=CI Test",
                "-c",
                "user.email=ci@example.invalid",
                *args,
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "--quiet", "--initial-branch=main")
    readme = tmp_path / "README.md"
    readme.write_text("Certified source\n")
    git("add", ".")
    git("commit", "--quiet", "-m", "Full certified baseline")
    certified = git("rev-parse", "HEAD")
    code = tmp_path / "docker/core/app/chat.py"
    code.parent.mkdir(parents=True)
    code.write_text("CHANGED = True\n")
    git("add", ".")
    git("commit", "--quiet", "-m", "Code push whose CI is cancelled")
    cancelled = git("rev-parse", "HEAD")
    readme.write_text("Certified source\nDocumentation follow-up\n")
    git("add", ".")
    git("commit", "--quiet", "-m", "Documentation push")
    head = git("rev-parse", "HEAD")
    unrelated = git("commit-tree", "HEAD^{tree}", "-m", "Unrelated history")
    monkeypatch.chdir(tmp_path)
    return certified, cancelled, head, unrelated


def successful_run(revision, run_id=100):
    return {
        "id": run_id,
        "run_attempt": 1,
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "head_branch": "main",
        "head_sha": revision,
        "head_repository": {"full_name": "example/klukai"},
    }


def successful_jobs(run):
    return {
        "total_count": len(FULL_JOB_NAMES),
        "jobs": [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "run_id": run["id"],
                "head_sha": run["head_sha"],
            }
            for name in sorted(FULL_JOB_NAMES)
        ],
    }


def baseline_api(runs, jobs=None):
    calls = []

    def api(path, token):
        assert token == "fixture-token"
        calls.append(path)
        if "/workflows/ci.yml/runs?" in path:
            assert path.startswith("/repos/example/klukai/")
            assert "branch=main" in path and "status=success" in path
            return {"workflow_runs": runs}
        for run in runs:
            if path == (
                f"/repos/example/klukai/actions/runs/{run['id']}"
                f"/attempts/{run['run_attempt']}/jobs?per_page=100"
            ):
                return jobs if jobs is not None else successful_jobs(run)
        raise AssertionError(f"Unexpected API request: {path}")

    return api, calls


def prior_full_baseline(head, api, **overrides):
    arguments = {
        "repository": "example/klukai",
        "ref": "refs/heads/main",
        "after": head,
        "current_run": "200",
        "token": "fixture-token",
        "api": api,
    }
    arguments.update(overrides)
    return selection.full_success_baseline(**arguments)


def test_cancelled_code_push_remains_selected_after_documentation_push(push_history):
    certified, cancelled, head, _ = push_history
    api, calls = baseline_api([successful_run(certified)])
    baseline = prior_full_baseline(head, api)
    assert baseline == certified
    assert selection.plan(selection.changed(cancelled, head))["core"] is False
    selected = selection.plan(selection.changed(baseline, head))
    assert selected == {
        "full": False,
        "core": True,
        "frontend": False,
        "components": False,
    }
    assert len(calls) == 2


def test_documentation_only_after_full_certification_stays_fast(push_history):
    _, certified, head, _ = push_history
    api, _ = baseline_api([successful_run(certified)])
    baseline = prior_full_baseline(head, api)
    assert selection.plan(selection.changed(baseline, head)) == {
        "full": False,
        "core": False,
        "frontend": False,
        "components": False,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", 200),
        ("id", "100/../../other"),
        ("run_attempt", 0),
        ("path", ".github/workflows/unrelated.yml"),
        ("event", "pull_request"),
        ("status", "in_progress"),
        ("conclusion", "cancelled"),
        ("head_branch", "another-branch"),
        ("head_repository", {"full_name": "other/klukai"}),
        ("head_sha", "not-a-sha"),
    ],
)
def test_baseline_rejects_unbound_or_incomplete_runs(push_history, field, value):
    certified, _, head, _ = push_history
    run = successful_run(certified)
    run[field] = value
    api, calls = baseline_api([run])
    assert prior_full_baseline(head, api) is None
    assert len(calls) == 1, "Rejected runs must not be used to request job evidence"


@pytest.mark.parametrize("revision", ["unrelated", "missing"])
def test_baseline_must_be_available_in_head_ancestry(push_history, revision):
    _, _, head, unrelated = push_history
    api, _ = baseline_api(
        [successful_run(unrelated if revision == "unrelated" else "f" * 40)]
    )
    assert prior_full_baseline(head, api) is None


@pytest.mark.parametrize(
    "fault",
    [
        "skipped",
        "failed",
        "cancelled",
        "running",
        "missing",
        "duplicate",
        "truncated",
        "wrong-run",
        "wrong-sha",
        "wrong-attempt",
    ],
)
def test_baseline_requires_complete_successful_full_job_evidence(push_history, fault):
    certified, _, head, _ = push_history
    run = successful_run(certified)
    jobs = successful_jobs(run)
    if fault in {"skipped", "failed", "cancelled"}:
        jobs["jobs"][0]["conclusion"] = fault
    elif fault == "running":
        jobs["jobs"][0]["status"] = "in_progress"
    elif fault == "missing":
        jobs["jobs"].pop()
        jobs["total_count"] -= 1
    elif fault == "duplicate":
        jobs["jobs"][-1] = copy.deepcopy(jobs["jobs"][0])
    elif fault == "truncated":
        jobs["total_count"] += 1
    elif fault == "wrong-run":
        jobs["jobs"][0]["run_id"] = 999
    elif fault == "wrong-attempt":
        jobs["jobs"][0]["run_attempt"] = 2
    else:
        jobs["jobs"][0]["head_sha"] = "f" * 40
    api, _ = baseline_api([run], jobs)
    assert prior_full_baseline(head, api) is None


def test_baseline_skips_routine_success_for_an_older_full_success(push_history):
    certified, cancelled, head, _ = push_history
    routine = successful_run(cancelled, 101)
    full = successful_run(certified)
    base_api, _ = baseline_api([routine, full])

    def api(path, token):
        result = base_api(path, token)
        if "/runs/101/attempts/1/jobs?" in path:
            for job in result["jobs"]:
                if job["name"] == "Required CI gates":
                    job["conclusion"] = "skipped"
        return result

    assert prior_full_baseline(head, api) == certified


@pytest.mark.parametrize(
    "failure",
    [
        OSError("unavailable"),
        ValueError("bad JSON"),
        http.client.IncompleteRead(b"partial"),
    ],
)
def test_baseline_lookup_errors_fall_back_to_full(push_history, failure):
    _, _, head, _ = push_history

    def api(path, token):
        raise failure

    assert prior_full_baseline(head, api) is None
    assert all(selection.plan(selection.changed("", head)).values())


def test_baseline_lookup_is_bounded_when_no_full_success_is_available(push_history):
    certified, _, head, _ = push_history
    runs = [successful_run(certified, run_id) for run_id in range(1000, 1100)]
    jobs = {"total_count": 0, "jobs": []}
    api, calls = baseline_api(runs, jobs)
    assert prior_full_baseline(head, api) is None
    assert len(calls) <= 21


@pytest.mark.parametrize(
    "overrides",
    [
        {"repository": "example/klukai/../other"},
        {"ref": "refs/heads/feature"},
        {"current_run": "invalid"},
        {"after": "invalid"},
        {"token": ""},
    ],
)
def test_invalid_lookup_identity_never_calls_github(push_history, overrides):
    _, _, head, _ = push_history
    api, calls = baseline_api([])
    assert prior_full_baseline(head, api, **overrides) is None
    assert calls == []


@pytest.mark.parametrize("available", [True, False])
def test_cumulative_cli_uses_certified_baseline_or_full_fallback(
    push_history, tmp_path, monkeypatch, available
):
    certified, cancelled, head, _ = push_history
    calls = []

    def baseline(**kwargs):
        calls.append(kwargs)
        return certified if available else None

    monkeypatch.setattr(selection, "full_success_baseline", baseline)
    output = tmp_path / "selected-output"
    for name, value in {
        "GITHUB_REPOSITORY": "example/klukai",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_RUN_ID": "200",
        "GH_TOKEN": "fixture-token",
        "GITHUB_OUTPUT": str(output),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        sys,
        "argv",
        ["selection", "plan", "--cumulative", "--before", cancelled, "--after", head],
    )
    selection.main()
    fields = dict(line.split("=", 1) for line in output.read_text().splitlines())
    selected = json.loads(fields["selected"])
    assert selected["core"] is True
    assert selected["full"] is (not available)
    assert calls == [
        {
            "repository": "example/klukai",
            "ref": "refs/heads/main",
            "after": head,
            "current_run": "200",
            "token": "fixture-token",
        }
    ]
