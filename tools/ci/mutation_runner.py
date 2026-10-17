"""Bounded snapshot execution and raw inventory extraction for mutmut 3.6.0."""

from __future__ import annotations

from collections import Counter
import configparser
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import Any
import uuid

from .mutation_evidence import (
    SCOPE,
    SENSORS,
    TOOL_VERSION,
    VIRTUALIZED,
    actual_status,
    digest,
    input_hashes,
    tool_identity,
    validate_report,
)

TEST_SELECTION = (
    "tests/test_affection_pure.py",
    "tests/test_affection_coverage.py",
    "tests/test_affection_system.py",
    "tests/test_affection_timeline.py",
    "tests/test_affection_mutants.py",
    "tests/test_audit_chain_mutants.py",
    "tests/test_speech_routing_mutants.py",
    "tests/test_speech_intimacy_gate.py",
    # Source-AST callsite counts inspect generated trampoline copies and are
    # verified by normal CI. Mutation execution selects every behavioral test.
    "tests/test_lm_gateway_auth.py::test_auth_header_reads_current_token_and_fails_closed",
    "tests/test_lm_gateway_auth.py::test_llm_json_calls_are_authenticated_without_secret_logs",
    "tests/test_lm_gateway_auth.py::test_affection_classifier_auth_preserves_prompt_and_ttl",
    "tests/test_lm_gateway_auth.py::test_memory_backfill_request_is_authenticated",
    "tests/test_lm_gateway_auth.py::test_standalone_memory_scripts_authenticate_requests",
    "tests/test_lm_gateway_auth.py::test_subsystem_health_authenticates_model_probe",
    "tests/test_security_integrity_fixes.py",
    "tests/test_personality_pure.py",
    "tests/test_personality_contract.py",
    "tests/test_critical_mutation_contracts.py",
    "tests/test_critical_speech_contracts.py",
)


HWMON = Path("/sys/class/hwmon")


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def cpu_flags() -> str:
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("flags"):
            return line
    return ""


def temperature_c() -> float | None:
    """The 70C ceiling protects bare-metal hosts; a VM guest has no host sensors."""
    values: list[float] = []
    for folder in HWMON.glob("hwmon*"):
        try:
            if (folder / "name").read_text().strip() not in (
                "coretemp",
                "k10temp",
                "zenpower",
            ):
                continue
            values.extend(int(p.read_text()) / 1000 for p in folder.glob("temp*_input"))
        except (OSError, ValueError):
            continue
    if not values:
        if "hypervisor" in cpu_flags().split():
            return None
        raise ValueError("CPU temperature telemetry is unavailable")
    return max(values)


def _hotter(peak: float | None) -> float | None:
    reading = temperature_c()
    if (reading is None) != (peak is None):
        raise ValueError("CPU thermal telemetry changed during mutation")
    return None if peak is None or reading is None else max(peak, reading)


def bounded_command(
    argv: list[str], cwd: Path, env: dict[str, str], output: Path, *, timeout: float
) -> tuple[int, float | None]:
    peak = temperature_c()
    if peak is not None and peak >= 70:
        raise ValueError("CPU already exceeds the 70C ceiling")
    started = time.monotonic()
    with output.open("w") as log:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            while proc.poll() is None:
                peak = _hotter(peak)
                if (peak is not None and peak >= 70) or (
                    time.monotonic() - started > timeout
                ):
                    raise ValueError(
                        "Mutation thermal/wall-clock safety budget exceeded"
                    )
                time.sleep(0.5)
            return proc.returncode, peak
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()


def _peak(peak: float | None, measured: float | None) -> float | None:
    if (peak is None) != (measured is None):
        raise ValueError("CPU thermal telemetry changed during mutation")
    return None if peak is None or measured is None else max(peak, measured)


def _snapshot(root: Path, output: Path) -> Path:
    snapshot = output / "snapshot"
    core = snapshot / "docker/core"
    core.mkdir(parents=True)
    for name in ("app", "tests"):
        shutil.copytree(
            root / "docker/core" / name,
            core / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )
    for name in (
        "setup.cfg",
        "pytest.ini",
        "seed_memories.py",
        "reannotate_existing.py",
    ):
        shutil.copy2(root / "docker/core" / name, core / name)
    (snapshot / "config").mkdir()
    shutil.copy2(root / "config/personality.yaml", snapshot / "config/personality.yaml")
    # Tests that intentionally inspect repository paths also run one level
    # below core in mutmut's generated tree. Both copies bind the same input.
    (snapshot / "docker/config").mkdir()
    shutil.copy2(
        root / "config/personality.yaml", snapshot / "docker/config/personality.yaml"
    )
    tools = snapshot / "tools/ci"
    tools.mkdir(parents=True)
    for path in (root / "tools/ci").glob("mutation*.py"):
        shutil.copy2(path, tools / path.name)
    return core


def _environment(core: Path, output: Path, run_id: str) -> dict[str, str]:
    env = os.environ.copy()
    for key in tuple(env):
        if key.endswith("API_KEY") or key in (
            "KLUKAI_MIGRATION_TEST_DATABASE_URL",
            "MUTANT_UNDER_TEST",
            "PYTEST_ADDOPTS",
        ):
            env.pop(key)
    env.update(
        {
            "KLUKAI_TEST_ALLOW_LIVE_BACKENDS": "0",
            "DATABASE_URL": "postgresql://invalid:invalid@127.0.0.1:1/invalid",
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "QDRANT_URL": "http://127.0.0.1:1",
            "PERSONALITY_PATH": str(core.parents[1] / "config/personality.yaml"),
            "PYTHONPATH": str(core.parents[1] / "tools/ci"),
            "PYTHONHASHSEED": "0",
            "TZ": "UTC",
            "KLUKAI_MUTATION_RUN_ID": run_id,
            "KLUKAI_MUTATION_OBSERVATIONS": str(output / "raw/observations"),
        }
    )
    return env


def _observations(output: Path) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    found: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for path in sorted((output / "raw/observations").glob("*.json")):
        proof = json.loads(path.read_text())
        found.setdefault(proof["mutant"], []).append(
            (str(path.relative_to(output)), proof)
        )
    return found


def _outcomes(core: Path, output: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    observations = _observations(output)
    metadata, mutants = {}, []
    for module in SCOPE:
        source = core / "mutants" / f"{module}.meta"
        target = output / "raw/metadata" / f"{module}.meta"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        metadata[module] = str(target.relative_to(output))
        meta = json.loads(target.read_text())
        for key, code in sorted(meta["exit_code_by_key"].items()):
            proofs = observations.get(key, [])
            name, proof = proofs[0] if len(proofs) == 1 else (None, None)
            mutants.append(
                {
                    "id": key,
                    "module": module,
                    "exit_code": code,
                    "status": actual_status(code, proof),
                    "observation": name,
                    "duration_seconds": meta["durations_by_key"].get(key),
                }
            )
    return metadata, mutants


def _generated_sources(core: Path, output: Path) -> dict[str, str]:
    found = {}
    for module in SCOPE:
        source = core / "mutants" / module
        target = output / "raw/generated" / module
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        found[module] = str(target.relative_to(output))
    return found


def execute(
    root: Path,
    output: Path,
    *,
    max_children: int = 2,
    development: bool = False,
    timeout: float = 3600,
    threshold: float = 0.8,
    max_age_days: int = 30,
) -> dict[str, Any]:
    root, output = root.resolve(), output.resolve()
    if output.exists() or output.is_relative_to(root):
        raise ValueError("Use a fresh private output directory outside the repository")
    if type(max_children) is not int or not 1 <= max_children <= 2:
        raise ValueError("At most two mutation children are permitted")
    validate_configuration(root / "docker/core")
    identity = tool_identity()
    if identity["version"] != TOOL_VERSION:
        raise ValueError(f"Only locked mutmut {TOOL_VERSION} is accepted")
    dirty = bool(git(root, "status", "--porcelain"))
    if dirty and not development:
        raise ValueError("Final mutation evidence requires an exact clean commit")
    output.mkdir(parents=True, mode=0o700)
    (output / "raw").mkdir()
    hashes = input_hashes(root)
    run_id = uuid.uuid4().hex
    report: dict[str, Any] = {
        "schema": 1,
        "run_id": run_id,
        "source_revision": git(root, "rev-parse", "HEAD"),
        "started_at": datetime.now(UTC).isoformat(),
        "development_only": development,
        "tool": identity,
        "scope": list(SCOPE),
        "inputs": hashes,
        "max_children": max_children,
        "complete": False,
        "commands": [],
    }
    core = _snapshot(root, output)
    env = _environment(core, output, run_id)
    peak = temperature_c()
    if peak is None:
        flags = output / "raw/cpu-flags.txt"
        flags.write_text(cpu_flags() + "\n")
        report.update(thermal_telemetry=VIRTUALIZED, cpu_flags="raw/cpu-flags.txt")
    else:
        report["thermal_telemetry"] = SENSORS
    try:
        baseline = [
            sys.executable,
            "-m",
            "pytest",
            *TEST_SELECTION,
            "-q",
            "-W",
            "error",
            "-p",
            "mutation_observer",
        ]
        code, measured = bounded_command(
            baseline, core, env, output / "raw/baseline.log", timeout=300
        )
        peak = _peak(peak, measured)
        report["commands"].append(
            {"argv": baseline, "exit_code": code, "output": "raw/baseline.log"}
        )
        observations = _observations(output).get("", [])
        if code != 0 or len(observations) != 1:
            raise ValueError("Clean baseline failed; no mutations will be counted")
        report["baseline"] = observations[0][0]
        command = [
            sys.executable,
            "-m",
            "mutmut",
            "run",
            "--max-children",
            str(max_children),
        ]
        code, measured = bounded_command(
            command, core, env, output / "raw/mutmut.log", timeout=timeout
        )
        peak = _peak(peak, measured)
        report["commands"].append(
            {"argv": command, "exit_code": code, "output": "raw/mutmut.log"}
        )
        report["metadata"], report["mutants"] = _outcomes(core, output)
        report["generated_sources"] = _generated_sources(core, output)
        report["complete"] = code == 0 and all(
            m["status"] in ("killed", "survived") for m in report["mutants"]
        )
        report["counts"] = dict(Counter(m["status"] for m in report["mutants"]))
    except (ValueError, OSError) as error:
        report["execution_error"] = str(error)
    finally:
        report.update(
            finished_at=datetime.now(UTC).isoformat(),
            cpu_temperature_max_c=peak,
            sources_unchanged=input_hashes(root) == hashes,
        )
        report["raw"] = {
            str(p.relative_to(output)): digest(p)
            for p in sorted((output / "raw").rglob("*"))
            if p.is_file()
        }
        (output / "mutation.json").write_text(json.dumps(report, indent=2))
    if report.get("execution_error"):
        raise ValueError(report["execution_error"])
    result = validate_report(
        output,
        root,
        report["source_revision"],
        allow_development=development,
        threshold=threshold,
        max_age_days=max_age_days,
    )
    return result


def validate_configuration(core: Path) -> None:
    config = configparser.ConfigParser()
    config.read(core / "setup.cfg")
    if not config.has_section("mutmut"):
        raise ValueError("Missing mutmut scope configuration")
    options = config["mutmut"]

    def lines(name: str) -> tuple[str, ...]:
        return tuple(
            line.strip() for line in options.get(name, "").splitlines() if line.strip()
        )

    if sorted(lines("source_paths")) != sorted(SCOPE):
        raise ValueError("Mutation configuration omits extracted critical logic")
    if lines("pytest_add_cli_args_test_selection") != TEST_SELECTION:
        raise ValueError(
            "Mutation test selection differs from the bound critical suite"
        )
    if options.getboolean("mutate_only_covered_lines", False):
        raise ValueError("Uncovered mutations cannot be silently excluded")
    for name in (
        "do_not_mutate",
        "do_not_mutate_patterns",
        "only_mutate",
        "type_check_command",
    ):
        if lines(name):
            raise ValueError(
                "Mutation exclusions/type-check-only outcomes are forbidden"
            )
