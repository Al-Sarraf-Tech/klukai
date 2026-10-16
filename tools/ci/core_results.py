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
        source = Coverage(
            data_file=None, config_file=str(config) if config.exists() else False
        )
        # Analyze source possibilities with no executed arcs. This is an
        # in-memory parser input, never submitted as observed execution data.
        parser_data = source.get_data()
        try:
            parser_data.add_arcs(
                {str(path.resolve()): [] for path in expected.values()}
            )
            for name, path in expected.items():
                pure = PurePosixPath(name)
                require(
                    not pure.is_absolute() and ".." not in pure.parts,
                    "Unsafe source path",
                )
                require(
                    path.is_file()
                    and not any(p.is_symlink() for p in (path, *path.parents)),
                    "Linked or missing runtime module",
                )
                for key, number in file_counts(path, files[name], source).items():
                    totals[key] = totals.get(key, 0) + number
        finally:
            parser_data.close(force=True)
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
        for element in root.iter():
            if element.tag in {"system-out", "system-err"}:
                require(
                    not re.search(r"\b\w*Warning:", element.text or ""),
                    "Unexpected test warning",
                )
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
        if root.tag == "testsuites":
            for key, expected in {
                "tests": len(identities),
                "failures": 0,
                "errors": 0,
                "skipped": skipped,
            }.items():
                if key in root.attrib:
                    require(
                        xml_count(root, key) == expected, "JUnit outer counter differs"
                    )
        require(
            passed > 0 and skipped <= 75, "Empty passing suite or skip ceiling exceeded"
        )
        return {"passed": passed, "skipped": skipped, "tests": len(identities)}
    except (OSError, ET.ParseError, ValueError) as exc:
        if isinstance(exc, InvalidResults):
            raise
        raise InvalidResults("Invalid core JUnit results") from exc


def raw_arc_results(path: Path, observations: list[tuple[int, int]]) -> None:
    """Validate every raw sysmon arc before Coverage JSON's lossy projection.

    Coupled intentionally to the pinned CPython/coverage collector. Compile and
    inspect trusted source only; never execute the resulting code objects.
    """
    import coverage
    import dis
    import platform
    from types import CodeType
    from coverage.bytecode import ALWAYS_JUMPS, BranchArcResolver, bytes_to_lines
    from coverage.parser import PythonParser

    require(
        coverage.__version__ == "7.16.2" and platform.python_version() == "3.14.8",
        "Raw arc semantics require pinned coverage7.16.2/CPython3.14.8",
    )
    parser = PythonParser(filename=str(path))
    parser.parse_source()
    possible = set(parser.arcs())
    pending = [
        compile(path.read_text(), str(path), "exec", dont_inherit=True, optimize=0)
    ]
    while pending:
        code = pending.pop()
        pending.extend(item for item in code.co_consts if isinstance(item, CodeType))
        if code.co_name == "__annotate__":
            continue  # The pinned collector explicitly ignores annotation code.
        lines = bytes_to_lines(code)
        possible.update((line, line) for line in lines.values())
        instructions = list(dis.get_instructions(code))
        offsets = {
            instruction.offset: index for index, instruction in enumerate(instructions)
        }
        successors: dict[int, set[int]] = {}
        exception_entries = dis.Bytecode(code).exception_entries
        resolver = BranchArcResolver(code, lines, parser.multiline_map)
        for index, instruction in enumerate(instructions):
            line = lines.get(instruction.offset)
            if instruction.opname == "RETURN_VALUE" and line is not None:
                possible.add((line, -code.co_firstlineno))
                possible.add((parser.first_line(line), -code.co_firstlineno))
            targets: set[int] = set()
            terminal = instruction.opname in {
                "RETURN_VALUE",
                "RETURN_GENERATOR",
                "RAISE_VARARGS",
                "RERAISE",
            }
            if instruction.opcode in dis.hasjump:
                targets.add(instruction.argval)
            if (
                not terminal
                and instruction.opcode not in ALWAYS_JUMPS
                and index + 1 < len(instructions)
            ):
                targets.add(instructions[index + 1].offset)
            targets.update(
                entry.target
                for entry in exception_entries
                if entry.start <= instruction.offset < entry.end
            )
            successors[instruction.offset] = targets
            if (
                instruction.opcode in dis.hasjump
                and instruction.opcode not in ALWAYS_JUMPS
            ):
                for target in targets:
                    if (
                        arc := resolver.resolve(instruction.offset, target)
                    ) is not None:
                        possible.add(arc)
        # A raw sysmon branch can expose an expression's individual lines,
        # while its resolver/AST report normalizes them. Retain both compiler
        # CFG edges and their source normalization; never connect across a
        # terminating return/raise merely because statements are adjacent.
        for offset, targets in successors.items():
            start = lines.get(offset)
            if start is None:
                continue
            pending_targets = list(targets)
            seen: set[int] = set()
            while pending_targets:
                target_offset = pending_targets.pop()
                if target_offset in seen or target_offset not in offsets:
                    continue
                seen.add(target_offset)
                end = lines.get(target_offset)
                if end is None:
                    pending_targets.extend(successors[target_offset])
                elif start > 0 and end > 0:
                    possible.add((start, end))
                    possible.update(parser.translate_arcs([(start, end)]))
    require(
        all(
            type(arc) is tuple
            and len(arc) == 2
            and all(type(line) is int for line in arc)
            and arc in possible
            for arc in observations
        ),
        f"Raw SQLite arc differs from trusted source control flow: {path.name}; {sorted(set(observations) - possible)[:8]}",
    )


def retained_results(
    core: Path, database: Path, original: Any, recomputed: Any
) -> dict[str, float | int]:
    """Reconstruct both reports from a private database copy and trusted source.

    Map only the producer's fixed /source/docker/core prefix. No retained Python is imported or executed.
    """
    import json
    import shutil
    import tempfile

    from .core_profile import coverage_database
    from .core_resources import ResourceFailure
    from .core_source import sha256

    before = sha256(database)
    try:
        coverage_database(database)
        expected = {str(p.resolve()) for p in (core / "app").rglob("*.py")}
        with tempfile.TemporaryDirectory(prefix="core-evidence-") as temporary:
            private = Path(temporary)
            copied = private / ".coverage"
            shutil.copyfile(database, copied)
            observed = Coverage(data_file=str(copied), config_file=False)
            mapped = Coverage(data_file=None, config_file=False)
            config = core / ".coveragerc"
            if config.exists():
                mapped = Coverage(data_file=None, config_file=str(config))
            raw, target = observed.get_data(), mapped.get_data()
            try:
                observed.load()
                require(raw.has_arcs(), "Retained SQLite has no arcs")

                def map_path(name: str) -> str:
                    prefix = "/source/docker/core/"
                    require(name.startswith(prefix), "Unexpected SQLite source path")
                    if name.startswith(prefix):
                        relative = PurePosixPath(name[len(prefix) :])
                        require(".." not in relative.parts, "Unsafe SQLite source path")
                        name = str(core.joinpath(*relative.parts).resolve())
                    require(name in expected, "Unexpected SQLite runtime module")
                    return name

                require(
                    {map_path(name) for name in raw.measured_files()} == expected,
                    "Retained SQLite module inventory differs",
                )
                for name in raw.measured_files():
                    raw_arc_results(Path(map_path(name)), raw.arcs(name) or [])
                target.update(raw, map_path=map_path)
                report = private / "reconstructed.json"
                mapped.json_report(outfile=str(report))
                actual = json.loads(report.read_text())
                actual["files"] = {
                    Path(name).resolve().relative_to(core.resolve()).as_posix(): value
                    for name, value in actual["files"].items()
                }
                # Source-derived possible arcs and actual observations must
                # agree with BOTH retained reports, including destinations.
                for candidate in (original, recomputed):
                    require(
                        candidate["files"] == actual["files"]
                        and candidate["totals"] == actual["totals"],
                        "Coverage JSON differs from independently reconstructed SQLite",
                    )
                return coverage_results(core, actual)
            finally:
                raw.close(force=True)
                target.close(force=True)
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        CoverageException,
        ResourceFailure,
    ) as exc:
        if isinstance(exc, InvalidResults):
            raise
        raise InvalidResults("Invalid retained core observations") from exc
    finally:
        require(sha256(database) == before, "Retained SQLite changed during validation")
