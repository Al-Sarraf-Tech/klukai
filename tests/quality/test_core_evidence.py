"""Unit fixtures use genuine executed coverage, never managed-core acceptance."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tools.ci import core_results


@pytest.fixture(scope="module")
def observed(tmp_path_factory):
    core = tmp_path_factory.mktemp("core-observations")
    (core / "app").mkdir()
    shutil.copyfile(
        Path(core_results.__file__).parents[2] / "docker/core/.coveragerc",
        core / ".coveragerc",
    )
    (core / "app/__init__.py").touch()
    (core / "app/domain.py").write_text(
        "def choose(value):\n    if value:\n        return 1\n    return 2\n"
    )
    (core / "tests").mkdir()
    (core / "tests/test_domain.py").write_text(
        "from app.domain import choose\ndef test_both():\n    assert choose(True)==1\n    assert choose(False)==2\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "tests/",
            "-q",
            "-W",
            "error",
            "-o",
            "asyncio_default_fixture_loop_scope=function",
            "-o",
            "asyncio_default_test_loop_scope=function",
            "--cov=app",
            "--cov-branch",
            "--cov-report=json:coverage.json",
            "--junitxml=junit.xml",
        ],
        cwd=core,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(core / ".coverage")) as db:
        db.execute(
            "UPDATE file SET path=REPLACE(path,?,?)", (str(core), "/source/docker/core")
        )
        db.commit()
    return core, json.loads((core / "coverage.json").read_text())


def test_reconstruct_real_sqlite_without_modifying_evidence(observed, tmp_path):
    core, data = observed
    before = (core / ".coverage").read_bytes()
    result = core_results.retained_results(
        core, core / ".coverage", data, copy.deepcopy(data)
    )
    assert result["branches_percent"] == result["statements_percent"] == 100
    assert (core / ".coverage").read_bytes() == before


@pytest.mark.parametrize(
    "fault",
    [
        "destination",
        "json-line",
        "empty-arcs",
        "missing-module",
        "low-statement",
        "low-branch",
    ],
)
def test_sqlite_and_json_must_agree_on_actual_observations(observed, tmp_path, fault):
    import sqlite3
    from contextlib import closing

    core, original = observed
    data = copy.deepcopy(original)
    database = tmp_path / ".coverage"
    shutil.copy2(core / ".coverage", database)
    if fault == "destination":
        data["files"]["app/domain.py"]["executed_branches"][0][1] = 1
    elif fault == "json-line":
        data["files"]["app/domain.py"]["executed_lines"].pop()
    elif fault == "missing-module":
        del data["files"]["app/__init__.py"]
    else:
        with closing(sqlite3.connect(database)) as db:
            if fault == "empty-arcs":
                db.execute("DELETE FROM arc")
            elif fault == "low-branch":
                db.execute("DELETE FROM arc WHERE fromno=2 AND tono=4")
            else:
                db.execute("DELETE FROM arc WHERE tono=4 OR fromno=4")
            db.commit()
    with pytest.raises(core_results.InvalidResults):
        core_results.retained_results(core, database, data, copy.deepcopy(data))


def test_producer_retains_only_verifiable_safe_image_fields():
    from tools.ci import core_runner

    info = {
        "Id": "sha256:" + "a" * 64,
        "Architecture": "amd64",
        "Os": "linux",
        "RepoDigests": [],
        "Config": {
            "User": "1000:1000",
            "Entrypoint": ["/opt/ci/bin/python"],
            "Env": ["SECRET=private", "PG_MAJOR=17"],
            "Labels": {
                "org.opencontainers.image.revision": "b" * 40,
                "cc.appnest.klukai.ci-lock-sha256": "c" * 64,
                "private": "secret-label",
            },
        },
    }
    result = core_runner.retained_image_identity(info)
    assert result == {
        "Id": info["Id"],
        "Architecture": "amd64",
        "Os": "linux",
        "RepoDigests": [],
        "Config": {
            "User": "1000:1000",
            "Entrypoint": ["/opt/ci/bin/python"],
            "Labels": {
                "org.opencontainers.image.revision": "b" * 40,
                "cc.appnest.klukai.ci-lock-sha256": "c" * 64,
            },
        },
    }
    assert core_runner.retained_image_identity(info, postgres=True)["PG_MAJOR"] == "17"
    assert "private" not in json.dumps(result)


def test_wrapper_only_never_establishes_full_core(tmp_path):
    from tools.ci import core_evidence

    (tmp_path / "TEST-1.json").write_text(json.dumps({"passed": True}))
    with pytest.raises(core_evidence.InvalidEvidence, match="manifest"):
        core_evidence.validate(tmp_path, tmp_path, "a" * 40)


@pytest.fixture
def full_bundle(tmp_path, observed, monkeypatch):
    from core_evidence_fixture import bundle

    return bundle(tmp_path, observed, monkeypatch)


def test_genuine_unit_measurements_with_synthetic_metadata_are_consumed(full_bundle):
    from tools.ci.core_evidence import validate

    root, output, revision = full_bundle
    result = validate(root, output, revision)
    assert result["coverage"]["branches_percent"] == 100
    assert result["junit"] == {"passed": 1, "skipped": 0, "tests": 1}


@pytest.mark.parametrize(
    "fault",
    [
        "stale",
        "future",
        "revision",
        "development",
        "source",
        "helper",
        "raw-missing",
        "raw-extra",
        "raw-modified",
        "image-os",
        "image-lock",
        "probe",
        "execution",
        "cleanup",
        "command-subset",
        "forged-summary",
        "junit-failure",
        "junit-skips",
        "junit-warning",
        "log-warning",
    ],
)
def test_full_bundle_rejects_forged_or_incomplete_proof(full_bundle, fault):
    from core_evidence_fixture import rehash
    from tools.ci.core_evidence import validate, InvalidEvidence
    from datetime import datetime, timedelta, timezone

    root, output, revision = full_bundle
    report = json.loads((output / "manifest.json").read_text())
    if fault in ["stale", "future"]:
        report["started_at"] = (
            datetime.now(timezone.utc) + timedelta(days=-31 if fault == "stale" else 1)
        ).isoformat()
    elif fault == "revision":
        report["source_revision"] = "a" * 40
    elif fault == "development":
        report["development"] = True
    elif fault in ["source", "helper"]:
        (
            root
            / (
                "docker/core/app/domain.py"
                if fault == "source"
                else "tools/ci/core_profile.py"
            )
        ).write_text("changed")
    elif fault == "raw-missing":
        (output / "docker-000.stdout").unlink()
    elif fault == "raw-extra":
        (output / "docker-999.stdout").write_text("extra")
    elif fault == "raw-modified":
        (output / "docker-000.stdout").write_text("truncated")
    elif fault.startswith("image-"):
        p = output / "image-identity.json"
        data = json.loads(p.read_text())
        if fault == "image-os":
            data["core"]["Os"] = "windows"
        else:
            data["core"]["Config"]["Labels"]["cc.appnest.klukai.ci-lock-sha256"] = (
                "0" * 64
            )
        p.write_text(json.dumps(data))
    elif fault == "probe":
        p = output / "results/executor-probe.json"
        data = json.loads(p.read_text())
        data["packages"].popitem()
        p.write_text(json.dumps(data))
    elif fault == "execution":
        p = output / "docker-commands.json"
        data = json.loads(p.read_text())
        next(r for r in data if r["argv"][1:3] == ["start", "--attach"])[
            "exit_code"
        ] = 7
        p.write_text(json.dumps(data))
    elif fault == "cleanup":
        p = output / "resources.json"
        data = json.loads(p.read_text())
        data["cleaned"] = False
        p.write_text(json.dumps(data))
    elif fault in ["command-subset", "forged-summary"]:
        p = output / "results/validation.json"
        data = json.loads(p.read_text())
        if fault == "command-subset":
            data["pytest_command"].append("-k=one")
        else:
            data["coverage"]["covered_lines"] += 1
        p.write_text(json.dumps(data))
        report["results"] = data
    elif fault.startswith("junit-"):
        import xml.etree.ElementTree as ET

        p = output / "results/junit.xml"
        tree = ET.parse(p)
        suite = tree.getroot().find("testsuite")
        case = suite.find("testcase")
        if fault == "junit-failure":
            ET.SubElement(case, "failure").text = "real failure"
        elif fault == "junit-warning":
            ET.SubElement(case, "system-err").text = "RuntimeWarning: never awaited"
        else:
            suite.set("skipped", "76")
        tree.write(p)
    else:
        (output / "results/pytest.log").write_text(
            "RuntimeWarning: never awaited\n1 passed in 0.02s\n"
        )
    (output / "manifest.json").write_text(json.dumps(report))
    if fault not in ["raw-missing", "raw-modified", "raw-extra"]:
        rehash(output)
    with pytest.raises(InvalidEvidence):
        validate(root, output, revision)


def test_junit_suite_warning_is_not_hidden_by_passing_cases(observed, tmp_path):
    import xml.etree.ElementTree as ET

    core, _ = observed
    tree = ET.parse(core / "junit.xml")
    ET.SubElement(
        tree.getroot().find("testsuite"), "system-err"
    ).text = "ResourceWarning: unclosed database"
    p = tmp_path / "junit.xml"
    tree.write(p)
    with pytest.raises(core_results.InvalidResults, match="warning"):
        core_results.junit_results(p)


def test_self_consistent_forged_branch_destinations_fail(observed, tmp_path):
    import sqlite3
    from contextlib import closing

    core, original = observed
    data = copy.deepcopy(original)
    database = tmp_path / ".coverage"
    shutil.copyfile(core / ".coverage", database)
    with closing(sqlite3.connect(database)) as db:
        db.execute("UPDATE arc SET tono=1 WHERE fromno=2 AND tono=3")
        db.commit()
    for pair in data["files"]["app/domain.py"]["executed_branches"]:
        if pair == [2, 3]:
            pair[1] = 1
    with pytest.raises(core_results.InvalidResults):
        core_results.retained_results(core, database, data, copy.deepcopy(data))


def test_original_evidence_changed_during_consumer_is_rejected(
    full_bundle, monkeypatch
):
    from tools.ci import core_evidence

    root, output, revision = full_bundle
    original = core_evidence.retained_results

    def change(*args):
        result = original(*args)
        (output / "docker-000.stderr").write_text("changed during validation")
        return result

    monkeypatch.setattr(core_evidence, "retained_results", change)
    with pytest.raises(
        core_evidence.InvalidEvidence, match="changed during validation"
    ):
        core_evidence.validate(root, output, revision)


def test_audit_routes_only_full_test1_to_semantic_consumer(full_bundle):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/quality"))
    import checks

    root, output, revision = full_bundle
    note = checks.core_tests(root, revision, 30, output)
    assert "100.00%" in note and "1 passed" in note


def test_sqlite_cannot_claim_host_runtime_paths(observed, tmp_path):
    import sqlite3
    from contextlib import closing

    core, data = observed
    database = tmp_path / ".coverage"
    shutil.copyfile(core / ".coverage", database)
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "UPDATE file SET path=REPLACE(path,?,?)", ("/source/docker/core", str(core))
        )
        db.commit()
    with pytest.raises(core_results.InvalidResults, match="path|module"):
        core_results.retained_results(core, database, data, copy.deepcopy(data))


@pytest.mark.parametrize("fault", ["entrypoint", "version"])
def test_actual_create_cannot_override_driver_or_replace_producer_command(
    full_bundle, fault
):
    from core_evidence_fixture import rehash
    from tools.ci.core_evidence import validate, InvalidEvidence

    root, output, revision = full_bundle
    p = output / "docker-commands.json"
    rows = json.loads(p.read_text())
    if fault == "entrypoint":
        row = next(
            r
            for r in rows
            if r["argv"][1] == "create" and r["argv"][2].endswith("-core")
        )
        index = row["argv"].index("--env=CI")
        row["argv"].insert(index, "--entrypoint=/other")
    else:
        row = next(r for r in rows if r["argv"][1] == "version")
        row["argv"] = ["docker", "run", "unexpected-image"]
    p.write_text(json.dumps(rows))
    rehash(output)
    with pytest.raises(InvalidEvidence):
        validate(root, output, revision)


def test_rehashed_unknown_raw_artifacts_are_not_actual_producer_output(full_bundle):
    from core_evidence_fixture import rehash
    from tools.ci.core_evidence import validate, InvalidEvidence

    root, output, revision = full_bundle
    (output / "results/unexpected.txt").write_text(
        "not produced by the full-core runner"
    )
    rehash(output)
    with pytest.raises(InvalidEvidence, match="raw|inventory"):
        validate(root, output, revision)


@pytest.mark.parametrize(
    "fault", ["execution-boolean", "summary-boolean", "root-counter"]
)
def test_strict_scalars_and_outer_junit_counters_are_not_summary_assertions(
    full_bundle, fault
):
    from core_evidence_fixture import rehash
    from tools.ci.core_evidence import validate, InvalidEvidence
    import xml.etree.ElementTree as ET

    root, output, revision = full_bundle
    p = output / "manifest.json"
    report = json.loads(p.read_text())
    if fault == "execution-boolean":
        report["execution"]["attach_exit_code"] = False
    elif fault == "summary-boolean":
        q = output / "results/validation.json"
        data = json.loads(q.read_text())
        data["junit"]["skipped"] = False
        q.write_text(json.dumps(data))
        report["results"] = data
    else:
        q = output / "results/junit.xml"
        tree = ET.parse(q)
        tree.getroot().set("tests", "999")
        tree.write(q)
    p.write_text(json.dumps(report))
    rehash(output)
    with pytest.raises(InvalidEvidence):
        validate(root, output, revision)
