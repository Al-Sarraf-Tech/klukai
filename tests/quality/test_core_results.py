"""The core gate checks actual measured modules and both coverage dimensions."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from tools.ci.core_results import InvalidResults, coverage_results, junit_results


@pytest.fixture(scope="module")
def measured(tmp_path_factory):
    core = tmp_path_factory.mktemp("measured")
    (core / "app").mkdir()
    (core / "app/__init__.py").touch()
    (core / "app/domain.py").write_text(
        "def choice(value):\n    if value:\n        return 1\n    return 2\n"
    )
    (core / "test_domain.py").write_text(
        "from app.domain import choice\n"
        "def test_both_paths():\n    assert choice(True)==1\n    assert choice(False)==2\n"
    )
    command = [
        sys.executable,
        "-m",
        "pytest",
        "test_domain.py",
        "-q",
        "-W",
        "error",
        "--cov=app",
        "--cov-branch",
        "--cov-report=json:coverage.json",
        "--junitxml=junit.xml",
    ]
    result = subprocess.run(
        command, cwd=core, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return core, json.loads((core / "coverage.json").read_text())


def test_actual_pytest_and_full_coverage_pass(measured):
    core, data = measured
    result = coverage_results(core, data)
    assert result["statements_percent"] == 100
    assert result["branches_percent"] == 100
    assert result["modules"] == 2
    assert junit_results(core / "junit.xml") == {"passed": 1, "skipped": 0, "tests": 1}


@pytest.mark.parametrize(
    "change",
    [
        "missing_module",
        "extra_module",
        "branches_off",
        "missing_lines",
        "missing_branch",
        "summary_lie",
        "totals_lie",
        "duplicate_line",
        "duplicate_arc",
        "unsafe_path",
        "boolean_count",
        "empty",
        "omitted_new_module",
        "file_symlink",
    ],
)
def test_invalid_or_incomplete_coverage_fails(measured, tmp_path, change):
    source, original = measured
    core = tmp_path / "core"
    (core / "app").mkdir(parents=True)
    for path in (source / "app").glob("*.py"):
        (core / "app" / path.name).write_bytes(path.read_bytes())
    data = copy.deepcopy(original)
    file = data["files"]["app/domain.py"]
    if change == "missing_module":
        del data["files"]["app/domain.py"]
    elif change == "extra_module":
        data["files"]["app/fake.py"] = file
    elif change == "branches_off":
        data["meta"]["branch_coverage"] = False
    elif change == "missing_lines":
        file["missing_lines"] = [file["executed_lines"].pop()]
        file["summary"].update(covered_lines=3, missing_lines=1)
        data["totals"].update(covered_lines=3, missing_lines=1)
    elif change == "missing_branch":
        file["missing_branches"] = [file["executed_branches"].pop()]
        file["summary"].update(covered_branches=1, missing_branches=1)
        data["totals"].update(covered_branches=1, missing_branches=1)
    elif change == "summary_lie":
        file["summary"]["covered_lines"] = 0
    elif change == "totals_lie":
        data["totals"]["num_statements"] = 999
    elif change == "duplicate_line":
        file["executed_lines"].append(file["executed_lines"][0])
    elif change == "duplicate_arc":
        file["executed_branches"].append(file["executed_branches"][0])
    elif change == "unsafe_path":
        data["files"]["../app/domain.py"] = data["files"].pop("app/domain.py")
    elif change == "boolean_count":
        file["summary"]["missing_lines"] = False
    elif change == "empty":
        data["files"] = {}
    elif change == "omitted_new_module":
        (core / "app/unmeasured.py").write_text("VALUE=1\n")
    elif change == "file_symlink":
        (core / "app/domain.py").unlink()
        (core / "app/domain.py").symlink_to(source / "app/domain.py")
    with pytest.raises(InvalidResults):
        coverage_results(core, data)


def test_present_module_with_suppressed_executable_statements_fails(measured, tmp_path):
    source, original = measured
    core = tmp_path / "core"
    (core / "app").mkdir(parents=True)
    for path in (source / "app").glob("*.py"):
        (core / "app" / path.name).write_bytes(path.read_bytes())
    (core / "app/hidden.py").write_text("def omitted():\n    return 42\n")
    data = copy.deepcopy(original)
    # Merely naming every module is insufficient if executable code silently
    # contributes no statements to the measured denominator.
    data["files"]["app/hidden.py"] = copy.deepcopy(data["files"]["app/__init__.py"])
    with pytest.raises(InvalidResults, match="statement inventory"):
        coverage_results(core, data)


def test_present_module_with_suppressed_branch_denominator_fails(measured, tmp_path):
    source, original = measured
    core = tmp_path / "core"
    (core / "app").mkdir(parents=True)
    for path in (source / "app").glob("*.py"):
        (core / "app" / path.name).write_bytes(path.read_bytes())
    (core / "app/hidden.py").write_bytes((core / "app/domain.py").read_bytes())
    data = copy.deepcopy(original)
    hidden = copy.deepcopy(data["files"]["app/domain.py"])
    hidden["executed_branches"] = []
    hidden["summary"].update(covered_branches=0, missing_branches=0, num_branches=0)
    data["files"]["app/hidden.py"] = hidden
    for key in ("covered_lines", "num_statements"):
        data["totals"][key] += hidden["summary"][key]
    with pytest.raises(InvalidResults, match="branch inventory"):
        coverage_results(core, data)


@pytest.mark.parametrize(
    "change", ["failure", "error", "empty", "duplicate", "counts", "skips", "warnings"]
)
def test_junit_rejects_failed_or_unproved_execution(measured, tmp_path, change):
    core, _ = measured
    tree = ET.parse(core / "junit.xml")
    suite = tree.getroot().find("testsuite")
    case = suite.find("testcase")
    if change in ("failure", "error"):
        ET.SubElement(case, change).text = "actual failure"
        suite.set(change + "s", "1")
    elif change == "empty":
        suite.remove(case)
        suite.set("tests", "0")
    elif change == "duplicate":
        suite.append(copy.deepcopy(case))
        suite.set("tests", "2")
    elif change == "counts":
        suite.set("failures", "1")
    elif change == "skips":
        for index in range(76):
            skipped = ET.SubElement(
                suite, "testcase", classname="test", name=f"skip{index}"
            )
            ET.SubElement(skipped, "skipped")
        suite.set("skipped", "76")
        suite.set("tests", "77")
    else:
        ET.SubElement(
            case, "system-err"
        ).text = "RuntimeWarning: coroutine was never awaited"
    path = tmp_path / "junit.xml"
    tree.write(path)
    with pytest.raises(InvalidResults):
        junit_results(path)
