"""Validate every mutation against immutable inputs and raw pytest observations."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from functools import lru_cache
import hashlib
from importlib import metadata
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any

SCOPE = (
    "app/affection.py",
    "app/affection_classifier.py",
    "app/affection_types.py",
    "app/audit_chain.py",
    "app/personality/speech.py",
)
TOOL_VERSION = "3.6.0"


@lru_cache(maxsize=4)
def _regenerate(sources: tuple[tuple[str, str], ...], tool_hash: str) -> dict[str, str]:
    """Derive the complete inventory from current source, without importing it."""
    program = """
import json, sys
from mutmut.mutation.file_mutation import mutate_file_contents
sources = json.load(sys.stdin)
json.dump({name: mutate_file_contents(name, code)[0]
           for name, code in sources}, sys.stdout)
"""
    with tempfile.TemporaryDirectory(prefix="klukai-mutation-inventory-") as name:
        # Ignore repository config filters and coverage: every mutation is required.
        Path(name, "setup.cfg").write_text("[mutmut]\nsource_paths=app\n")
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", program],
                input=json.dumps(sources),
                text=True,
                capture_output=True,
                cwd=name,
                timeout=60,
                check=True,
            )
        except (subprocess.SubprocessError, OSError) as error:
            raise ValueError("Pinned-tool inventory regeneration failed") from error
    generated = object_dict(json.loads(result.stdout))
    if set(generated) != {module for module, _ in sources} or not all(
        isinstance(code, str) for code in generated.values()
    ):
        raise ValueError("Invalid independently generated mutation source")
    return {module: str(code) for module, code in generated.items()}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def input_hashes(root: Path) -> dict[str, str]:
    paths: set[Path] = set()
    for folder in ("docker/core/app", "docker/core/tests"):
        paths.update((root / folder).rglob("*.py"))
    paths.update((root / "tools/ci").glob("mutation*.py"))
    for name in (
        "docker/core/setup.cfg",
        "docker/core/pytest.ini",
        "docker/core/requirements-ci.txt",
        "config/personality.yaml",
    ):
        if (root / name).is_file():
            paths.add(root / name)
    return {str(path.relative_to(root)): digest(path) for path in sorted(paths)}


def tool_identity() -> dict[str, str]:
    dist = metadata.distribution("mutmut")
    hasher = hashlib.sha256()
    for path in sorted(dist.files or ()):
        if str(path).startswith("mutmut/") and str(path).endswith(".py"):
            hasher.update(
                str(path).encode() + b"\0" + dist.locate_file(path).read_bytes()
            )
    return {"name": "mutmut", "version": dist.version, "sha256": hasher.hexdigest()}


def object_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Expected an evidence object")
    return value


def integer(value: object, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("Invalid execution count")
    return value


def _raw_bytes(directory: Path, report: dict[str, Any], name: object) -> bytes:
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError("Missing relative raw-evidence path")
    path = (directory / name).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("Raw evidence escapes the report directory")
    expected = object_dict(report.get("raw")).get(name)
    if not path.is_file() or path.stat().st_size == 0 or digest(path) != expected:
        raise ValueError("Raw evidence is missing, empty or altered")
    return path.read_bytes()


def raw_document(
    directory: Path, report: dict[str, Any], name: object
) -> dict[str, Any]:
    return object_dict(json.loads(_raw_bytes(directory, report, name)))


def _commands(directory: Path, report: dict[str, Any]) -> None:
    commands = report.get("commands")
    if not isinstance(commands, list) or len(commands) != 2:
        raise ValueError("Missing actual baseline/mutation commands")
    for index, value in enumerate(commands):
        command = object_dict(value)
        if type(command.get("exit_code")) is not int or command["exit_code"] != 0:
            raise ValueError("Baseline or mutation execution command failed")
        argv = command.get("argv")
        if not isinstance(argv, list) or not all(
            isinstance(s, str) and s for s in argv
        ):
            raise ValueError("Missing actual command arguments")
        if index == 0:
            if len(argv) < 4 or argv[1:3] != ["-m", "pytest"]:
                raise ValueError("Baseline must execute the real pytest suite")
        elif argv[1:] != [
            "-m",
            "mutmut",
            "run",
            "--max-children",
            str(report["max_children"]),
        ]:
            raise ValueError("Mutation command/tool/worker count does not match")
        _raw_bytes(directory, report, command.get("output"))


def _period(value: dict[str, Any]) -> tuple[datetime, datetime]:
    try:
        start, end = (
            datetime.fromisoformat(value[key]) for key in ("started_at", "finished_at")
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Missing execution timestamps") from error
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise ValueError("Invalid timestamp ordering/timezone")
    return start, end


def _timestamps(report: dict[str, Any], max_age_days: int) -> None:
    start, end = _period(report)
    now = datetime.now(UTC)
    if end > now + timedelta(minutes=1) or now - start > timedelta(days=max_age_days):
        raise ValueError("Stale or future mutation evidence")


def _observation_identity(
    proof: dict[str, Any], report: dict[str, Any], mutant: str
) -> None:
    run_id = report.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("Missing unique execution identity")
    if proof.get("run_id") != run_id or proof.get("mutant") != mutant:
        raise ValueError("Pytest observation belongs to another run or mutant")
    start, end = _period(report)
    observed_start, observed_end = _period(proof)
    if observed_start < start or observed_end > end:
        raise ValueError("Raw pytest proof is stale, future or outside this execution")


def _identity(
    report: dict[str, Any], root: Path, revision: str, allow_development: bool
) -> None:
    if (
        not re.fullmatch(r"[a-f0-9]{40}", revision)
        or report.get("source_revision") != revision
    ):
        raise ValueError("Mutation report source revision mismatch")
    if report.get("schema") != 1 or report.get("sources_unchanged") is not True:
        raise ValueError("Source changed during execution or unsupported schema")
    if report.get("development_only") is not False and not (
        allow_development and report.get("development_only") is True
    ):
        raise ValueError("Development evidence cannot certify CI")
    if (
        report.get("tool") != tool_identity()
        or report["tool"].get("version") != TOOL_VERSION
    ):
        raise ValueError("Missing, mismatched or unpinned mutation tool")
    if sorted(report.get("scope", [])) != sorted(SCOPE):
        raise ValueError("Critical scope must include the extracted classifier")
    if report.get("inputs") != input_hashes(root):
        raise ValueError("Source, tests or configuration hashes do not match")
    for name in SCOPE:
        if f"docker/core/{name}" not in report["inputs"]:
            raise ValueError("Missing critical source input")
    children = integer(report.get("max_children"), 1)
    temperature = report.get("cpu_temperature_max_c")
    if (
        children > 2
        or isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(temperature)
        or not 0 < temperature < 70
    ):
        raise ValueError("Mutation resource/thermal budget was not proved")


def _baseline(directory: Path, report: dict[str, Any]) -> None:
    proof = raw_document(directory, report, report.get("baseline"))
    _observation_identity(proof, report, "")
    if (
        proof.get("mutant") != ""
        or type(proof.get("exit_code")) is not int
        or proof["exit_code"] != 0
    ):
        raise ValueError("Clean baseline did not pass")
    if actual_status(0, proof) != "survived":
        raise ValueError("Baseline test/collection failure cannot establish kills")


def _generated_keys(directory: Path, report: dict[str, Any], module: str) -> set[str]:
    sources = object_dict(report.get("generated_sources"))
    try:
        tree = ast.parse(_raw_bytes(directory, report, sources.get(module)))
    except SyntaxError as error:
        raise ValueError("Generated mutation source is invalid") from error
    prefix = module.removesuffix(".py").replace("/", ".") + "."
    return {
        prefix + node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and re.fullmatch(r".+__mutmut_[0-9]+", node.name)
    }


def _inventory(
    directory: Path, report: dict[str, Any], root: Path
) -> dict[str, tuple[str, int | None]]:
    metadata_files = object_dict(report.get("metadata"))
    if set(metadata_files) != set(SCOPE) or set(
        object_dict(report.get("generated_sources"))
    ) != set(SCOPE):
        raise ValueError("Raw mutation inventory omits a critical module")
    expected = _regenerate(
        tuple(
            (module, (root / "docker/core" / module).read_text()) for module in SCOPE
        ),
        report["tool"]["sha256"],
    )
    inventory = {}
    for module, name in metadata_files.items():
        generated = report["generated_sources"][module]
        if _raw_bytes(directory, report, generated) != expected[module].encode():
            raise ValueError(
                "Generated source differs from pinned-tool complete inventory"
            )
        raw = raw_document(directory, report, name)
        outcomes = object_dict(raw.get("exit_code_by_key"))
        if not outcomes:
            raise ValueError("Empty raw mutation inventory")
        if set(outcomes) != _generated_keys(directory, report, module):
            raise ValueError("Raw inventory omits generated mutants")
        for key, code in outcomes.items():
            if (
                not isinstance(key, str)
                or not key.startswith(
                    module.removesuffix(".py").replace("/", ".") + "."
                )
                or key in inventory
            ):
                raise ValueError("Invalid or duplicate raw mutant identity")
            if code is not None and type(code) is not int:
                raise ValueError("Invalid raw mutation exit code")
            inventory[key] = (module, code)
    return inventory


def _phase(value: object) -> tuple[str, str]:
    report = object_dict(value)
    phase, outcome = report.get("when"), report.get("outcome")
    duration = report.get("duration")
    if (
        phase not in ("setup", "call", "teardown")
        or outcome not in ("passed", "failed", "skipped")
        or not isinstance(report.get("nodeid"), str)
        or not report["nodeid"]
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration < 0
    ):
        raise ValueError("Invalid raw pytest phase")
    if outcome == "failed" and (
        not isinstance(report.get("longrepr"), str) or not report["longrepr"]
    ):
        raise ValueError("Missing actual failure traceback")
    return str(phase), str(outcome)


def _phase_counts(proof: dict[str, Any]) -> tuple[int, int, int]:
    errors = 0
    for key in ("collection_errors", "internal_errors"):
        failures = proof.get(key)
        if not isinstance(failures, list) or not all(
            isinstance(error, str) and error for error in failures
        ):
            raise ValueError("Missing raw pytest error observations")
        errors += len(failures)
    reports = proof.get("reports")
    if not isinstance(reports, list):
        raise ValueError("Missing raw pytest test-phase observations")
    phases = [_phase(value) for value in reports]
    calls = sum(phase == "call" and outcome != "skipped" for phase, outcome in phases)
    failed_calls = phases.count(("call", "failed"))
    errors += sum(phase != "call" and outcome == "failed" for phase, outcome in phases)
    expected = calls, failed_calls, errors
    observed = tuple(
        integer(proof.get(key)) for key in ("calls", "failed_calls", "errors")
    )
    if expected != observed or calls > integer(proof.get("collected"), 1):
        raise ValueError("Execution counts differ from raw pytest phases")
    return expected


def actual_status(exit_code: int | None, proof: dict[str, Any] | None) -> str:
    """Tool exit 3 is an internal error; mutmut itself mislabels it a kill."""
    if exit_code in (-24, 24, 36, 152, 255):
        return "timeout"
    if exit_code is None:
        return "unexecuted"
    if exit_code in (5, 33, 34):
        return "no_tests"
    if (
        proof is None
        or exit_code not in (0, 1)
        or type(proof.get("exit_code")) is not int
        or proof.get("exit_code") != exit_code
    ):
        return "error"
    try:
        calls, failures, errors = _phase_counts(proof)
    except ValueError:
        return "error"
    if errors or not calls:
        return "error"
    if exit_code == 1 and failures > 0:
        return "killed"
    if exit_code == 0 and failures == 0:
        return "survived"
    return "error"


def validate_report(
    directory: Path,
    root: Path,
    revision: str,
    *,
    threshold: float = 0.8,
    max_age_days: int = 30,
    allow_development: bool = False,
) -> dict[str, Any]:
    report = object_dict(json.loads((directory / "mutation.json").read_text()))
    _timestamps(report, max_age_days)
    _identity(report, root, revision, allow_development)
    _commands(directory, report)
    _baseline(directory, report)
    inventory = _inventory(directory, report, root)
    items = report.get("mutants")
    if report.get("complete") is not True or not isinstance(items, list) or not items:
        raise ValueError("Mutation execution is incomplete or empty")
    seen, killed = set(), 0
    for value in items:
        item = object_dict(value)
        key = item.get("id")
        if not isinstance(key, str) or key in seen or key not in inventory:
            raise ValueError("Duplicate, missing or invalid mutant identity")
        seen.add(key)
        module, code = inventory[key]
        if (
            item.get("module") != module
            or type(item.get("exit_code")) is not int
            or item.get("exit_code") != code
        ):
            raise ValueError("Mutant outcome does not match the raw inventory")
        proof = raw_document(directory, report, item.get("observation"))
        _observation_identity(proof, report, key)
        status = actual_status(code, proof)
        if item.get("status") != status or status not in ("killed", "survived"):
            raise ValueError("Mutation includes errors, timeouts or unexecuted cases")
        killed += status == "killed"
    if seen != set(inventory):
        raise ValueError("Report denominator omits generated mutants")
    rate = killed / len(inventory)
    if not 0.8 <= threshold <= 1 or rate < threshold:
        raise ValueError(
            f"Actual kill rate {killed}/{len(inventory)} ({rate:.2%}) below {threshold:.0%}"
        )
    return {
        "killed": killed,
        "total": len(inventory),
        "kill_rate": rate,
        "development_only": report["development_only"],
    }
