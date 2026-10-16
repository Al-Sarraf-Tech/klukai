"""Semantically validate core coverage and JUnit, without trusting percentages.

Source/executor/command binding belongs to the producer manifest. These checks
reconcile the measured file inventory, line/branch observations and test cases.
"""

from __future__ import annotations

import math
from pathlib import Path, PurePosixPath
import re
from typing import Any
import xml.etree.ElementTree as ET

from coverage import Coverage
from coverage.exceptions import CoverageException


class InvalidResults(ValueError):
    """Results do not establish the complete passing core gate."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise InvalidResults(message)


def count(value: Any) -> int:
    require(type(value) is int and value >= 0, "Invalid coverage count")
    return int(value)


def line_set(value: Any, size: int) -> set[int]:
    require(isinstance(value, list), "Missing line observations")
    require(
        all(type(line) is int and 0 < line <= size for line in value),
        "Invalid source line observation",
    )
    require(len(value) == len(set(value)), "Duplicate line observation")
    return set(value)


def arcs(value: Any, size: int) -> set[tuple[int, int]]:
    require(isinstance(value, list), "Missing branch observations")
    found: set[tuple[int, int]] = set()
    for pair in value:
        require(
            isinstance(pair, list)
            and len(pair) == 2
            and all(type(line) is int for line in pair)
            and 0 < pair[0] <= size
            and 0 < abs(pair[1]) <= size,
            "Invalid source branch observation",
        )
        found.add((pair[0], pair[1]))
    require(len(found) == len(value), "Duplicate branch observation")
    return found


def file_counts(path: Path, data: dict[str, Any], source: Coverage) -> dict[str, int]:
    size = len(path.read_text().splitlines())
    executed = line_set(data["executed_lines"], size)
    missing = line_set(data["missing_lines"], size)
    excluded = line_set(data["excluded_lines"], size)
    require(not (executed & missing or missing & excluded), "Conflicting line results")
    _, statements, source_excluded, _, _ = source.analysis2(str(path))
    require(
        (executed - excluded) | missing == set(statements)
        and excluded == set(source_excluded),
        "Measured statement inventory differs from actual source",
    )
    covered_arcs = arcs(data["executed_branches"], size)
    missing_arcs = arcs(data["missing_branches"], size)
    require(not covered_arcs & missing_arcs, "Conflicting branch results")
    # Public coverage.py branch analysis derives exits from the source rather
    # than accepting a report that silently assigns zero branches to a module.
    exits = {line: total for line, (total, _) in source.branch_stats(str(path)).items()}
    observed_exits: dict[int, int] = {}
    for start, _ in covered_arcs | missing_arcs:
        observed_exits[start] = observed_exits.get(start, 0) + 1
    require(
        exits == observed_exits, "Measured branch inventory differs from actual source"
    )
    observed = {
        "covered_lines": len(executed - excluded),
        "missing_lines": len(missing),
        "num_statements": len((executed - excluded) | missing),
        "excluded_lines": len(excluded),
        "covered_branches": len(covered_arcs),
        "missing_branches": len(missing_arcs),
        "num_branches": len(covered_arcs | missing_arcs),
    }
    require(
        all(count(data["summary"][key]) == number for key, number in observed.items()),
        "Coverage summary differs from observed lines/branches",
    )
    return observed


def coverage_results(core: Path, data: Any) -> dict[str, float | int]:
    try:
        require(data["meta"]["branch_coverage"] is True, "Branch coverage not enabled")
        files = data["files"]
        require(isinstance(files, dict) and files, "Empty measured source inventory")
        expected = {
            path.relative_to(core).as_posix(): path
            for path in (core / "app").rglob("*.py")
        }
        require(
            set(files) == set(expected), "Measured runtime module inventory differs"
        )
        totals: dict[str, int] = {}
        config = core / ".coveragerc"
        source = Coverage(config_file=str(config) if config.exists() else False)
        # Analyze source possibilities with no executed arcs. This is an
        # in-memory parser input, never submitted as observed execution data.
        source.get_data().add_arcs(
            {str(path.resolve()): [] for path in expected.values()}
        )
        for name, path in expected.items():
            pure = PurePosixPath(name)
            require(
                not pure.is_absolute() and ".." not in pure.parts, "Unsafe source path"
            )
            require(
                path.is_file()
                and not any(p.is_symlink() for p in (path, *path.parents)),
                "Linked or missing runtime module",
            )
            for key, number in file_counts(path, files[name], source).items():
                totals[key] = totals.get(key, 0) + number
        require(
            all(count(data["totals"][key]) == number for key, number in totals.items()),
            "Coverage totals differ from measured modules",
        )
        require(
            totals["num_statements"] > 0 and totals["num_branches"] > 0,
            "Empty statement or branch denominator",
        )
        statements = 100 * totals["covered_lines"] / totals["num_statements"]
        branches = 100 * totals["covered_branches"] / totals["num_branches"]
        require(
            statements >= 95 and branches >= 95,
            "Statement AND branch coverage must each reach95%",
        )
        return {
            "modules": len(files),
            "statements_percent": statements,
            "branches_percent": branches,
            **totals,
        }
    except (OSError, KeyError, TypeError, AttributeError, CoverageException) as exc:
        raise InvalidResults("Invalid core coverage results") from exc


def xml_count(element: ET.Element, key: str) -> int:
    value = element.get(key, "")
    require(re.fullmatch(r"[0-9]+", value), "Missing/invalid JUnit counter")
    return int(value)


def junit_results(path: Path) -> dict[str, int]:
    try:
        require(path.stat().st_size <= 64 * 1024**2, "JUnit exceeds size limit")
        raw = path.read_bytes()
        require(b"<!DOCTYPE" not in raw and b"<!ENTITY" not in raw, "Unsafe JUnit XML")
        root = ET.fromstring(raw)
        require(root.tag in {"testsuite", "testsuites"}, "Invalid JUnit root")
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        require(
            suites and not any(s.find("testsuite") is not None for s in suites),
            "Empty/nested JUnit suite",
        )
        identities: set[tuple[str, str]] = set()
        skipped = 0
        for suite in suites:
            require(
                xml_count(suite, "errors") == xml_count(suite, "failures") == 0,
                "JUnit reports failed execution",
            )
            cases = list(suite.findall("testcase"))
            require(xml_count(suite, "tests") == len(cases), "JUnit test count differs")
            suite_skips = 0
            for case in cases:
                identity = (case.get("classname", ""), case.get("name", ""))
                require(
                    all(identity) and identity not in identities,
                    "Missing/duplicate JUnit case identity",
                )
                identities.add(identity)
                require(
                    case.find("failure") is None and case.find("error") is None,
                    "Failed JUnit test case",
                )
                duration = float(case.get("time", "0"))
                require(
                    math.isfinite(duration) and duration >= 0, "Invalid test duration"
                )
                require(len(case.findall("skipped")) <= 1, "Duplicate skipped result")
                suite_skips += case.find("skipped") is not None
                for stream in ("system-out", "system-err"):
                    text = case.findtext(stream, "")
                    require(
                        not re.search(r"\b\w*Warning:", text), "Unexpected test warning"
                    )
            require(
                xml_count(suite, "skipped") == suite_skips, "JUnit skip count differs"
            )
            skipped += suite_skips
        passed = len(identities) - skipped
        require(
            passed > 0 and skipped <= 75, "Empty passing suite or skip ceiling exceeded"
        )
        return {"passed": passed, "skipped": skipped, "tests": len(identities)}
    except (OSError, ET.ParseError, ValueError) as exc:
        if isinstance(exc, InvalidResults):
            raise
        raise InvalidResults("Invalid core JUnit results") from exc
