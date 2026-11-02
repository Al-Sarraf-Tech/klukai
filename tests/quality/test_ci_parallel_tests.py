"""CI runs tests in parallel on every vCPU a job's VM has (owner, 2026-11-01)."""

from __future__ import annotations

from pathlib import Path
import re

import yaml

from tools.ci import core_evidence, core_profile, frontend_evidence

ROOT = Path(__file__).resolve().parents[2]
SERIAL_VCPUS = 2  # ops/amarillo/ci-runner/klukai_ci_runner.py KINDS["serial"]
FRONTEND_VCPUS = 4  # KINDS["frontend"]


def _workflow_job(name: str) -> dict:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    return workflow["jobs"][name]


def test_runner_profiles_still_match_the_worker_counts():
    source = (ROOT / "ops/amarillo/ci-runner/klukai_ci_runner.py").read_text()
    assert f'"serial": ({SERIAL_VCPUS}, ' in source
    assert f'"frontend": ({FRONTEND_VCPUS}, ' in source


def test_core_suite_runs_one_worker_per_container_cpu():
    assert f"'-n', '{SERIAL_VCPUS}'" in core_profile.DRIVER
    flags = core_profile.core_flags(Path("/s"), Path("/r"), "net")
    assert f"--cpus={SERIAL_VCPUS}" in flags


def test_core_evidence_requires_the_command_the_driver_runs():
    source = Path(core_evidence.__file__).read_text()
    assert re.search(rf'"-n",\s*"{SERIAL_VCPUS}",', source)


def test_ops_suite_uses_the_workers_only_where_the_step_asks():
    makefile = (ROOT / "Makefile").read_text()
    assert re.search(
        r"^test-ops:\n\t.*ops/amarillo/tests.*\$\(PYTEST_PARALLEL\)$", makefile, re.M
    )
    assert re.search(r"^PYTEST_PARALLEL \?=\s*$", makefile, re.M)
    step = next(
        row
        for row in _workflow_job("component-shards")["steps"]
        if "make test-ops" in row.get("run", "")
    )
    assert f'PYTEST_PARALLEL="-n {SERIAL_VCPUS}"' in step["run"]


def test_flutter_vm_suite_uses_every_frontend_cpu():
    flag = f"--concurrency={FRONTEND_VCPUS}"
    assert flag in frontend_evidence.COMMAND_FLAGS["vm"]
    assert f"platform_args=({flag})" in (ROOT / "scripts/ci/frontend.sh").read_text()


def test_flutter_chrome_suite_does_not_get_an_ignored_flag():
    # Flutter 3.44 prints "-j/--concurrency was parsed but will be ignored" for
    # web tests, and that line breaks the machine stream the evidence requires.
    assert not any(
        flag.startswith("--concurrency")
        for flag in frontend_evidence.COMMAND_FLAGS["chrome"]
    )
    source = (ROOT / "scripts/ci/frontend.sh").read_text()
    assert "platform_args=(--platform chrome)" in source


def test_xdist_is_hash_locked_for_every_job_that_uses_it():
    lock = (ROOT / "docker/core/requirements-ci.txt").read_text()
    assert re.search(r"^pytest-xdist==\S+ \\\n\s+--hash=sha256:", lock, re.M)
    assert re.search(r"^execnet==\S+ \\\n\s+--hash=sha256:", lock, re.M)
