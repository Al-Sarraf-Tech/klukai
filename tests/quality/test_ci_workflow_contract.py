"""Pre-merge gates require authorized exact source and actual pinned producers."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
REVISION = "a" * 40
LEGACY_NAMES = {
    "components": "Isolated component suites",
    "test": "Test Suite",
    "lint": "Lint",
    "typecheck": "Type Check (mypy)",
    "security": "Security Scan (bandit + safety)",
    "image-scan": "Image Scan (trivy)",
    "perf-gate": "Perf Gate (p99 regression check)",
    "frontend": "Frontend validation and immutable PWA evidence",
}


ROUTED = {group: "${{ fromJSON(needs.source-policy.outputs.labels_%s) }}" % group
          for group in ("build", "serial", "frontend")}
MOVABLE_JOBS = {"ci-image": "build", "test": "serial", "real-backends": "serial",
                "components": "serial", "frontend": "frontend"}


def test_the_router_only_ever_yields_amarillo_or_dominus_one_job_vm_labels():
    from tools.ci import pool

    routed = workflow()["jobs"]["source-policy"]["outputs"]
    for group in ("build", "serial", "frontend"):
        assert routed[f"labels_{group}"] == "${{ steps.route.outputs.labels_%s }}" % group
    for beat in ("0", "1800000000"):
        plan = pool.plan(beat, 1800000010)
        for group in ("build", "serial", "frontend"):
            assert json.loads(plan[f"labels_{group}"]) == [
                "self-hosted", "Linux", "X64", f"klukai-{plan['pool']}-{group}"]
    route = next(s for s in workflow()["jobs"]["source-policy"]["steps"] if s.get("id") == "route")
    assert route["env"]["DOMINUS_HEARTBEAT"] == "${{ vars.KLUKAI_CI_DOMINUS_HEARTBEAT }}"
    assert "${{" not in route["run"]  # the variable enters through the environment


def workflow():
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())


def block(job, step_id):
    return next(step["run"] for step in job["steps"] if step.get("id") == step_id)


def run_block(script, **environment):
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env={"PATH": os.environ["PATH"], **environment},
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_only_exact_dispatched_heads_and_main_pushes_start_self_hosted_ci():
    data = workflow()
    events = data.get("on", data.get(True))
    assert set(events) == {"push", "workflow_dispatch"}
    assert events["push"]["branches"] == ["main"]
    requested = events["workflow_dispatch"]["inputs"]["expected_revision"]
    assert requested["required"] is True and requested["type"] == "string"
    assert data["permissions"] == {"contents": "read"}


@pytest.mark.parametrize(
    "event,ref,actual,expected,allowed",
    [
        ("workflow_dispatch", "refs/heads/owner-reviewed", REVISION, REVISION, True),
        ("push", "refs/heads/main", REVISION, "", True),
        ("workflow_dispatch", "refs/heads/reviewed", REVISION, "b" * 40, False),
        ("workflow_dispatch", "refs/heads/reviewed", REVISION, "", False),
        ("workflow_dispatch", "refs/heads/reviewed", "invalid", "invalid", False),
        ("workflow_dispatch", "refs/tags/v1.2.3", REVISION, REVISION, False),
        ("workflow_dispatch", "refs/heads/reviewed", REVISION, "$(exit 0)", False),
        ("push", "refs/heads/unreviewed", REVISION, REVISION, False),
        ("pull_request", "refs/pull/1/merge", REVISION, REVISION, False),
        ("pull_request_target", "refs/heads/main", REVISION, REVISION, False),
    ],
)
def test_source_admission_executes_fail_closed(event, ref, actual, expected, allowed):
    job = workflow()["jobs"]["source-policy"]
    assert job["steps"][0]["id"] == "admit"
    assert all("uses" not in step for step in job["steps"][:1])
    script = block(job, "admit")
    assert "${{" not in script, "Source metadata must enter through quoted environment"
    result = run_block(
        script,
        EVENT_NAME=event,
        SOURCE_REF=ref,
        SOURCE_REVISION=actual,
        EXPECTED_REVISION=expected,
    )
    assert (result.returncode == 0) is allowed, result.stdout + result.stderr


def test_existing_required_names_are_preserved_and_every_job_is_aggregated():
    jobs = workflow()["jobs"]
    assert all(jobs[key]["name"] == value for key, value in LEGACY_NAMES.items())
    required = jobs["all-required"]
    assert required["if"] == "always() && needs.source-policy.outputs.full == 'true'"
    assert set(required["needs"]) == set(jobs) - {"all-required", "affected-required"}
    for key, job in jobs.items():
        labels = job["runs-on"]
        if isinstance(labels, str):
            # Owner, 2026-10-22: these groups run on Dominus when its pool is
            # online (tools/ci/pool.py), else on amarillo; see the router test.
            assert key in MOVABLE_JOBS and labels == ROUTED[MOVABLE_JOBS[key]], key
            continue
        # Owner decision 2026-10-19: everything else runs on amarillo's one-job VMs.
        assert labels in (
            ["self-hosted", "Linux", "X64", "klukai-amarillo-heavy"],
            ["self-hosted", "Linux", "X64", "klukai-amarillo-small"],
            ["self-hosted", "Linux", "X64", "klukai-amarillo-medium"],
            ["self-hosted", "Linux", "X64", "klukai-amarillo-build"],
            ["self-hosted", "Linux", "X64", "klukai-amarillo-serial"],
            ["self-hosted", "Linux", "X64", "klukai-amarillo-frontend"],
        )
        if key not in {"source-policy", "all-required", "affected-required"}:
            dependencies = job["needs"]
            if isinstance(dependencies, str):
                dependencies = [dependencies]
            assert "source-policy" in dependencies or "ci-image" in dependencies
        assert not job.get("continue-on-error")
        for step in job["steps"]:
            assert not step.get("continue-on-error")
            action = step.get("uses", "")
            assert (
                not action
                or action.startswith("./")
                or re.fullmatch(r"[^@]+@[a-f0-9]{40}", action)
            )
            assert not action.startswith(
                ("actions/upload-artifact", "actions/download-artifact")
            )


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "failed",
        "cancelled",
        "skipped",
        "missing",
        "empty",
        "extra",
        "no-result",
    ],
)
@pytest.mark.parametrize("optimization", ["0", "1", "2"])
def test_required_gate_cannot_turn_failed_cancelled_or_skipped_jobs_green(
    fault, optimization
):
    job = workflow()["jobs"]["all-required"]
    outcomes = {key: {"result": "success"} for key in job["needs"]}
    first = next(iter(outcomes))
    if fault in {"failed", "cancelled", "skipped"}:
        outcomes[first]["result"] = "failure" if fault == "failed" else fault
    elif fault == "missing":
        del outcomes[first]
    elif fault == "empty":
        outcomes.clear()
    elif fault == "extra":
        outcomes["unexpected"] = {"result": "success"}
    elif fault == "no-result":
        outcomes[first] = {}
    result = run_block(
        block(job, "verify"),
        NEEDS_JSON=json.dumps(outcomes),
        REQUIRED_JOBS=job["env"]["REQUIRED_JOBS"],
        PYTHONOPTIMIZE=optimization,
    )
    assert (result.returncode == 0) is (fault == "none"), result.stdout + result.stderr


@pytest.mark.parametrize("optimization", ["0", "1", "2"])
@pytest.mark.parametrize(
    "changes,removed,allowed",
    [
        ({}, None, True),
        ({"passed": False}, None, False),
        ({"passed": 1}, None, False),
        ({"certifying": False}, None, False),
        ({"source_unchanged": False}, None, False),
        ({"source_revision": "b" * 40}, None, False),
        ({"image_id": "sha256:" + "c" * 63}, None, False),
        ({"image_id": "sha256:" + "c" * 64 + "\n"}, None, False),
        ({}, "image_id", False),
    ],
    ids=[
        "valid",
        "failed",
        "nonboolean-pass",
        "noncertifying",
        "changed-source",
        "wrong-revision",
        "short-image-id",
        "newline-image-id",
        "missing-image-id",
    ],
)
def test_image_handoff_rejects_invalid_manifests_before_export(
    tmp_path, optimization, changes, removed, allowed
):
    output = tmp_path / "private output"
    output.mkdir(mode=0o700)
    manifest = {
        "passed": True,
        "certifying": True,
        "source_unchanged": True,
        "source_revision": REVISION,
        "image_id": "sha256:" + "c" * 64,
    }
    manifest.update(changes)
    if removed:
        del manifest[removed]
    (output / "manifest.json").write_text(json.dumps(manifest))
    tools = tmp_path / "fake producer tools"
    tools.mkdir()
    python = tools / "python3"
    # Stub only the Docker-owning producer boundary; execute the full unchanged
    # shell step and its real manifest-validation Python under each optimization.
    python.write_text(
        "#!/bin/sh\nset -eu\n"
        'if [ "$1" = -m ]; then\n'
        '  test "$#" = 10\n'
        '  test "$2" = tools.ci.tool_image\n'
        '  test "$3" = --root && test "$4" = "$GITHUB_WORKSPACE"\n'
        '  test "$5" = --output && test "$6" = "$KLUKAI_CI_OUTPUT"\n'
        '  test "$7" = --revision && test "$8" = "$KLUKAI_CI_REVISION"\n'
        '  test "$9" = --timeout && test "${10}" = 1500\n'
        "  exit 0\nfi\n"
        'test "$#" = 1 && test "$1" = -\n'
        'exec "$TASK_INLINE_PYTHON" -B "$@"\n'
    )
    python.chmod(0o700)
    selected = tmp_path / "selected image output"
    result = run_block(
        block(workflow()["jobs"]["ci-image"], "build"),
        PATH=str(tools) + os.pathsep + os.environ["PATH"],
        TASK_INLINE_PYTHON=sys.executable,
        GITHUB_WORKSPACE=str(ROOT),
        KLUKAI_CI_OUTPUT=str(output),
        KLUKAI_CI_REVISION=REVISION,
        GITHUB_OUTPUT=str(selected),
        PYTHONOPTIMIZE=optimization,
    )
    assert (result.returncode == 0) is allowed, result.stdout + result.stderr
    if allowed:
        assert selected.read_text() == "image_id=sha256:" + "c" * 64 + "\n"
    else:
        assert not selected.exists(), "Rejected manifests must never export an image"


def test_core_and_real_backends_use_one_source_bound_tool_image():
    jobs = workflow()["jobs"]
    build = jobs["ci-image"]
    script = "\n".join(step.get("run", "") for step in build["steps"])
    assert "python3 -m tools.ci.tool_image" in script
    assert build["needs"] == "source-policy"
    assert (
        next(step for step in build["steps"] if "uses" in step)["with"]["fetch-depth"]
        == 0
    )
    for name, command in [
        ("test", "scripts/ci/core.sh"),
        ("real-backends", "scripts/ci/backends.sh"),
    ]:
        job = jobs[name]
        assert "services" not in job
        # source-policy publishes the pool; both stay with ci-image's executor image.
        assert job["needs"] == ["source-policy", "ci-image"]
        assert job["runs-on"] == ROUTED["serial"] and jobs["ci-image"]["runs-on"] == ROUTED["build"]
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        assert command in commands and "pip install" not in commands
        assert (
            job["env"]["KLUKAI_CORE_CI_IMAGE"]
            == "${{ needs.ci-image.outputs.image_id }}"
        )
        assert "@sha256:" in job["env"]["KLUKAI_CORE_POSTGRES_IMAGE"]
        if name == "real-backends":
            assert "@sha256:" in job["env"]["KLUKAI_CORE_REDIS_IMAGE"]
            # Memory lives in PostgreSQL only: the job provisions no vector store.
            assert not any("QDRANT" in key for key in job["env"])


def test_node_archive_checksum_failure_stops_before_unpack_or_path_selection(tmp_path):
    action = yaml.safe_load((ROOT / ".github/actions/node/action.yml").read_text())
    script = action["runs"]["steps"][0]["run"]
    tools = tmp_path / "fake tools"
    tools.mkdir()
    curl = tools / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = --output ]; then printf corrupt > "$2"; exit 0; fi\n'
        "  shift\ndone\nexit 2\n"
    )
    tar = tools / "tar"
    tar.write_text('#!/bin/sh\nprintf called > "$TASK_TAR_SENTINEL"\nexit 0\n')
    curl.chmod(0o700)
    tar.chmod(0o700)
    runner = tmp_path / "runner with spaces"
    runner.mkdir()
    selected_path = tmp_path / "selected path"
    sentinel = tmp_path / "unpacked"
    result = run_block(
        script,
        PATH=str(tools) + os.pathsep + os.environ["PATH"],
        RUNNER_TEMP=str(runner),
        GITHUB_PATH=str(selected_path),
        TASK_TAR_SENTINEL=str(sentinel),
    )
    assert result.returncode != 0
    assert "FAILED" in result.stdout + result.stderr
    assert not sentinel.exists(), "A corrupt archive must never reach tar"
    assert not selected_path.exists(), "An unverified toolchain must never enter PATH"


def test_only_admitted_source_can_execute_affected_gate_checker():
    job = workflow()["jobs"]["affected-required"]
    assert job["if"] == "always() && needs.source-policy.result == 'success'"
    source = workflow()["jobs"]["source-policy"]
    assert source["steps"][0]["id"] == "admit"
    assert "uses" not in source["steps"][0]


def test_release_build_requires_full_ci_for_its_exact_revision_before_building():
    data = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    assert data["permissions"]["actions"] == "read"
    steps = data["jobs"]["build"]["steps"]
    gate = next(i for i, row in enumerate(steps) if row.get("id") == "full-ci")
    build = next(i for i, row in enumerate(steps) if row.get("name") == "Build image")
    assert gate < build
    assert steps[gate]["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "green_run" in steps[gate]["run"]
    assert "os.environ['GITHUB_SHA']" in steps[gate]["run"]


def test_component_only_push_can_run_after_intentionally_skipped_tool_image():
    from tools.ci.selection import plan, required

    selected = plan(["ops/amarillo/tests/test_ci_runner.py"])
    expected = required(selected)
    assert "components" in expected and "ci-image" not in expected
    job = workflow()["jobs"]["components"]
    assert job["needs"] == ["source-policy", "ci-image"]
    assert (
        "always() && !cancelled() && needs.source-policy.result == 'success'"
        in job["if"]
    )
    assert (
        'contains(fromJSON(\'["success","skipped"]\'), needs.ci-image.result)'
        in job["if"]
    )
    assert job["runs-on"] == ROUTED["serial"]


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_every_main_push_and_dispatch_plans_full_certification(event, tmp_path):
    """Deploy-on-merge deploys only fully certified commits, so a merge to main
    must never run a selective plan that skips Flutter, the PWA build or the
    image gates (a skipped gate is not release certification)."""
    fake = tmp_path / "python3"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    script = block(workflow()["jobs"]["source-policy"], "select")
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env={
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "EVENT_NAME": event,
            "BEFORE_REVISION": "b" * 40,
            "GITHUB_SHA": REVISION,
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "--full" in result.stdout.split()


def test_frontend_job_logs_the_pwa_digest_the_deployer_binds():
    script = (ROOT / "scripts/ci/frontend.sh").read_text()
    assert "KLUKAI_PWA_SHA256=" in script
