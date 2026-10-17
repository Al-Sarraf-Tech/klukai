"""Mutation acceptance requires actual executions and intact bound inputs."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
import subprocess
import sys

import pytest

from tools.ci import mutation

REVISION = "a" * 40
MODULES = (
    "app/affection.py",
    "app/affection_classifier.py",
    "app/affection_types.py",
    "app/audit_chain.py",
    "app/personality/speech.py",
)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="session")
def generated_sample(tmp_path_factory):
    directory = tmp_path_factory.mktemp("pinned-mutmut-sample")
    (directory / "setup.cfg").write_text("[mutmut]\nsource_paths=app\n")
    source = 'def critical():\n    return "value"\n'
    code = """
import json, sys
from mutmut.mutation.file_mutation import mutate_file_contents
json.dump(mutate_file_contents("app/sample.py", sys.stdin.read()), sys.stdout)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        input=source,
        text=True,
        capture_output=True,
        cwd=directory,
        check=True,
        timeout=30,
    )
    generated, names = json.loads(result.stdout)
    assert names == ["x_critical__mutmut_1", "x_critical__mutmut_2"]
    return source, generated, names


@pytest.fixture
def bundle(tmp_path, generated_sample):
    root, output = tmp_path / "repo", tmp_path / "proof"
    source, generated_code, mutant_names = generated_sample
    for name in MODULES:
        p = root / "docker/core" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(source)
    for name in [
        "docker/core/setup.cfg",
        "docker/core/pytest.ini",
        "docker/core/tests/test_critical.py",
        "config/personality.yaml",
    ]:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("test-bound-input")
    hashes = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }
    now = datetime.now(UTC)
    report = {
        "schema": 1,
        "run_id": "d" * 32,
        "source_revision": REVISION,
        "started_at": (now - timedelta(minutes=1)).isoformat(),
        "finished_at": now.isoformat(),
        "development_only": False,
        "sources_unchanged": True,
        "tool": mutation.tool_identity(),
        "max_children": 2,
        "cpu_temperature_max_c": 51.0,
        "scope": list(MODULES),
        "inputs": hashes,
        "complete": True,
        "raw": {},
        "metadata": {},
        "generated_sources": {},
        "baseline": "raw/baseline.json",
        "mutants": [],
    }
    report["commands"] = [
        {
            "argv": ["python", "-m", "pytest", "tests/test_critical.py"],
            "exit_code": 0,
            "output": "raw/baseline.log",
        },
        {
            "argv": ["python", "-m", "mutmut", "run", "--max-children", "2"],
            "exit_code": 0,
            "output": "raw/mutmut.log",
        },
    ]
    for command in report["commands"]:
        log = output / command["output"]
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("Actual retained command output\n")
        report["raw"][command["output"]] = hashlib.sha256(log.read_bytes()).hexdigest()
    baseline = {
        "mutant": "",
        "exit_code": 0,
        "collected": 1,
        "calls": 1,
        "failed_calls": 0,
        "errors": 0,
        "collection_errors": [],
        "internal_errors": [],
        "reports": [
            {
                "nodeid": "tests/test_critical.py::test_case",
                "when": "call",
                "outcome": "passed",
                "duration": 0.01,
                "longrepr": "",
            }
        ],
        "run_id": report["run_id"],
        "started_at": report["started_at"],
        "finished_at": report["finished_at"],
    }
    report["raw"][report["baseline"]] = write_json(
        output / report["baseline"], baseline
    )
    metas = {name: {"exit_code_by_key": {}} for name in MODULES}
    for i in range(10):
        module = MODULES[i // 2]
        key = f"{module.removesuffix('.py').replace('/', '.')}.{mutant_names[i % 2]}"
        killed = i < 8
        observation = {
            "mutant": key,
            "exit_code": int(killed),
            "collected": 1,
            "calls": 1,
            "failed_calls": int(killed),
            "errors": 0,
            "collection_errors": [],
            "internal_errors": [],
            "reports": [
                {
                    "nodeid": "tests/test_critical.py::test_case",
                    "when": "call",
                    "outcome": "failed" if killed else "passed",
                    "duration": 0.01,
                    "longrepr": "AssertionError: independent contract"
                    if killed
                    else "",
                }
            ],
            "run_id": report["run_id"],
            "started_at": report["started_at"],
            "finished_at": report["finished_at"],
        }
        raw = f"raw/observed-{i}.json"
        report["raw"][raw] = write_json(output / raw, observation)
        report["mutants"].append(
            {
                "id": key,
                "module": module,
                "exit_code": int(killed),
                "status": "killed" if killed else "survived",
                "observation": raw,
            }
        )
        metas[module]["exit_code_by_key"][key] = int(killed)
    for name, meta in metas.items():
        raw = f"raw/{name}.meta"
        report["raw"][raw] = write_json(output / raw, meta)
        report["metadata"][name] = raw
        generated = f"raw/generated/{name}"
        target = output / generated
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(generated_code)
        report["raw"][generated] = hashlib.sha256(target.read_bytes()).hexdigest()
        report["generated_sources"][name] = generated
    write_json(output / "mutation.json", report)
    return root, output, report


def validate(bundle, **kwargs):
    root, output, report = bundle
    write_json(output / "mutation.json", report)
    return mutation.validate_report(output, root, REVISION, **kwargs)


def rewrite_raw(bundle, name, update):
    _, output, report = bundle
    value = json.loads((output / name).read_text())
    update(value)
    report["raw"][name] = write_json(output / name, value)


def test_exact_threshold_uses_actual_per_mutant_outcomes(bundle):
    result = validate(bundle)
    assert result["killed"] == 8
    assert result["total"] == 10
    assert result["kill_rate"] == 0.8


@pytest.mark.parametrize(
    "status,code",
    [("timeout", 36), ("unexecuted", None), ("no_tests", 5), ("error", 3)],
)
def test_non_execution_never_counts_as_a_kill(bundle, status, code):
    item = bundle[2]["mutants"][0]
    item.update(status=status, exit_code=code)
    with pytest.raises(ValueError):
        validate(bundle)


@pytest.mark.parametrize(
    "field,value",
    [
        ("errors", 1),
        ("collected", 0),
        ("calls", 0),
        ("failed_calls", 0),
        ("exit_code", 3),
    ],
)
def test_tool_killed_label_is_not_execution_proof(bundle, field, value):
    rewrite_raw(bundle, "raw/observed-0.json", lambda raw: raw.update({field: value}))
    with pytest.raises(ValueError):
        validate(bundle)


@pytest.mark.parametrize(
    "field,value", [("exit_code", 1), ("errors", 1), ("collected", 0), ("calls", 0)]
)
def test_failed_baseline_cannot_establish_a_mutant_kill(bundle, field, value):
    rewrite_raw(bundle, "raw/baseline.json", lambda raw: raw.update({field: value}))
    with pytest.raises(ValueError):
        validate(bundle)


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "omitted_survivor",
        "duplicate",
        "missing_classifier",
        "stale",
        "future",
        "revision",
        "tool",
        "tool_hash",
        "input_hash",
        "changed_sources",
        "development",
        "below_threshold",
        "thermal",
        "concurrency",
        "incomplete",
    ],
)
def test_invalid_or_misbound_reports_fail_closed(bundle, change):
    root, _, report = bundle
    if change == "empty":
        report["mutants"] = []
    elif change == "omitted_survivor":
        report["mutants"].pop()
    elif change == "duplicate":
        report["mutants"].append(report["mutants"][0].copy())
    elif change == "missing_classifier":
        report["scope"].remove("app/affection_classifier.py")
    elif change == "stale":
        report["finished_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    elif change == "future":
        report["finished_at"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    elif change == "revision":
        report["source_revision"] = "c" * 40
    elif change == "tool":
        report["tool"]["version"] = "3.5.0"
    elif change == "tool_hash":
        report["tool"]["sha256"] = ""
    elif change == "input_hash":
        (root / "docker/core/app/affection.py").write_text("changed")
    elif change == "changed_sources":
        report["sources_unchanged"] = False
    elif change == "development":
        report["development_only"] = True
    elif change == "below_threshold":
        report["mutants"][0]["status"] = "survived"
    elif change == "thermal":
        report["cpu_temperature_max_c"] = 70
    elif change == "concurrency":
        report["max_children"] = 3
    elif change == "incomplete":
        report["complete"] = False
    with pytest.raises(ValueError):
        validate(bundle)


def _virtualized(bundle, flags="flags\t: fpu sse2 hypervisor avx2"):
    _, output, report = bundle
    path = output / "raw/cpu-flags.txt"
    path.write_text(flags + "\n")
    report["raw"]["raw/cpu-flags.txt"] = hashlib.sha256(path.read_bytes()).hexdigest()
    report.update(
        thermal_telemetry="virtualized-guest",
        cpu_flags="raw/cpu-flags.txt",
        cpu_temperature_max_c=None,
    )


def test_virtualized_guest_proves_thermal_safety_by_its_cpu_flags(bundle):
    _virtualized(bundle)
    assert validate(bundle)["total"] == len(bundle[2]["mutants"])


@pytest.mark.parametrize("change", ["no_hypervisor", "reading", "unknown", "tamper"])
def test_virtualized_thermal_claims_fail_closed(bundle, change):
    if change == "no_hypervisor":
        _virtualized(bundle, "flags\t: fpu sse2 avx2")
    else:
        _virtualized(bundle)
    _, output, report = bundle
    if change == "reading":
        report["cpu_temperature_max_c"] = 50.0
    elif change == "unknown":
        report["thermal_telemetry"] = "estimated"
    elif change == "tamper":
        (output / "raw/cpu-flags.txt").write_text("flags\t: hypervisor sse2\n")
    with pytest.raises(ValueError):
        validate(bundle)


def test_runner_treats_a_sensorless_vm_as_virtualized_and_bare_metal_as_unsafe(
    tmp_path, monkeypatch
):
    from tools.ci import mutation_runner

    monkeypatch.setattr(mutation_runner, "HWMON", tmp_path / "hwmon-root")
    monkeypatch.setattr(mutation_runner, "cpu_flags", lambda: "flags : sse2 hypervisor")
    assert mutation_runner.temperature_c() is None
    monkeypatch.setattr(mutation_runner, "cpu_flags", lambda: "flags : sse2")
    with pytest.raises(ValueError):
        mutation_runner.temperature_c()
    sensor = tmp_path / "hwmon-root/hwmon0"
    sensor.mkdir(parents=True)
    (sensor / "name").write_text("k10temp\n")
    (sensor / "temp1_input").write_text("48000\n")
    assert mutation_runner.temperature_c() == 48.0
    with pytest.raises(ValueError):
        mutation_runner._hotter(None)  # telemetry appeared mid-run
    with pytest.raises(ValueError):
        mutation_runner._peak(None, 48.0)


def test_raw_evidence_tampering_fails(bundle):
    (bundle[1] / "raw/observed-0.json").write_text("{}")
    with pytest.raises(ValueError):
        validate(bundle)


def test_evidence_paths_cannot_escape_the_bundle(bundle):
    report = bundle[2]
    report["baseline"] = "../other.json"
    report["raw"]["../other.json"] = "b" * 64
    with pytest.raises(ValueError):
        validate(bundle)


def test_development_mode_is_explicit_and_does_not_certify_ci(bundle):
    bundle[2]["development_only"] = True
    with pytest.raises(ValueError):
        validate(bundle)
    assert validate(bundle, allow_development=True)["development_only"] is True


@pytest.mark.parametrize(
    "field,value",
    [("collected", True), ("calls", True), ("failed_calls", True), ("exit_code", True)],
)
def test_boolean_values_are_not_execution_counts(bundle, field, value):
    rewrite_raw(bundle, "raw/observed-0.json", lambda raw: raw.update({field: value}))
    with pytest.raises(ValueError):
        validate(bundle)


def test_another_mutants_observation_cannot_prove_a_kill(bundle):
    rewrite_raw(
        bundle, "raw/observed-0.json", lambda raw: raw.update(mutant="another-mutant")
    )
    with pytest.raises(ValueError):
        validate(bundle)


@pytest.mark.parametrize(
    "body,expected_code,expected_status",
    [
        ("def test_case():\n    assert 2 + 2 == 4\n", 0, "survived"),
        ("def test_case():\n    assert 2 + 2 == 5\n", 1, "killed"),
        (
            "import pytest\n@pytest.fixture(autouse=True)\ndef broken_setup():\n    raise RuntimeError('setup failed')\ndef test_case():\n    assert True\n",
            1,
            "error",
        ),
        (
            "import pytest\n@pytest.fixture(autouse=True)\ndef broken_teardown():\n    yield\n    raise RuntimeError('teardown failed')\ndef test_case():\n    assert True\n",
            1,
            "error",
        ),
        ("def broken syntax\n", 2, "error"),
    ],
)
def test_observer_distinguishes_real_call_failures_from_test_errors(
    tmp_path, body, expected_code, expected_status
):
    import os
    import subprocess
    import sys
    from tools.ci.mutation_evidence import actual_status

    case = tmp_path / "test_case.py"
    case.write_text(body)
    observations = tmp_path / "observations"
    env = os.environ.copy()
    env.update(
        PYTHONPATH=str(Path(__file__).resolve().parents[2] / "tools/ci"),
        KLUKAI_MUTATION_RUN_ID="controlled-pytest-fixture",
        KLUKAI_MUTATION_OBSERVATIONS=str(observations),
        MUTANT_UNDER_TEST="app.audit_chain.x_function__mutmut_1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "mutation_observer", str(case)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=15,
    )
    assert proc.returncode == expected_code, proc.stdout.decode()
    files = list(observations.glob("*.json"))
    assert len(files) == 1
    proof = json.loads(files[0].read_text())
    assert proof["mutant"] == "app.audit_chain.x_function__mutmut_1"
    assert actual_status(proc.returncode, proof) == expected_status


def test_fresh_finish_does_not_hide_a_stale_execution_start(bundle):
    bundle[2]["started_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    with pytest.raises(ValueError):
        validate(bundle)


@pytest.mark.parametrize(
    "change", ["missing", "failed", "wrong_tool", "missing_log", "wrong_workers"]
)
def test_actual_baseline_and_mutmut_commands_are_required(bundle, change):
    report = bundle[2]
    if change == "missing":
        report.pop("commands")
    elif change == "failed":
        report["commands"][1]["exit_code"] = 3
    elif change == "wrong_tool":
        report["commands"][1]["argv"][2] = "another_tool"
    elif change == "missing_log":
        report["commands"][1]["output"] = "missing.log"
    elif change == "wrong_workers":
        report["commands"][1]["argv"][-1] = "3"
    with pytest.raises(ValueError):
        validate(bundle)


@pytest.mark.parametrize(
    "option,value",
    [
        ("source_paths", "app/affection.py"),
        ("mutate_only_covered_lines", "true"),
        ("do_not_mutate", "app/affection.py"),
        ("do_not_mutate_patterns", "logger"),
        ("only_mutate", "app/audit_chain.py"),
        ("type_check_command", "mypy app"),
        ("pytest_add_cli_args_test_selection", "tests/only_one_test.py"),
    ],
)
def test_configuration_cannot_reduce_critical_scope_or_disable_mutants(
    tmp_path, option, value
):
    from tools.ci.mutation_runner import TEST_SELECTION, validate_configuration
    from tools.ci.mutation_evidence import SCOPE
    import configparser

    config = configparser.ConfigParser()
    config["mutmut"] = {
        "source_paths": "\n".join(SCOPE),
        "pytest_add_cli_args_test_selection": "\n".join(TEST_SELECTION),
        option: value,
    }
    with (tmp_path / "setup.cfg").open("w") as output:
        config.write(output)
    with pytest.raises(ValueError):
        validate_configuration(tmp_path)


@pytest.mark.parametrize("name", ["raw/baseline.json", "raw/observed-0.json"])
@pytest.mark.parametrize("change", ["another_run", "stale_execution", "after_finish"])
def test_raw_pytest_proof_cannot_be_replayed_or_mixed_from_another_run(
    bundle, name, change
):
    def alter(proof):
        if change == "another_run":
            proof["run_id"] = "e" * 32
        elif change == "stale_execution":
            proof["started_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
            proof["finished_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
        else:
            proof["started_at"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
            proof["finished_at"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()

    rewrite_raw(bundle, name, alter)
    with pytest.raises(ValueError):
        validate(bundle)


def test_shortened_metadata_cannot_remove_a_generated_survivor(bundle):
    report = bundle[2]
    omitted = report["mutants"].pop()
    name = report["metadata"][omitted["module"]]
    rewrite_raw(bundle, name, lambda meta: meta["exit_code_by_key"].pop(omitted["id"]))
    with pytest.raises(ValueError):
        validate(bundle)


def test_coordinated_report_metadata_and_generated_source_omission_is_rejected(bundle):
    import ast

    _, output, report = bundle
    omitted = report["mutants"].pop()
    rewrite_raw(
        bundle,
        report["metadata"][omitted["module"]],
        lambda meta: meta["exit_code_by_key"].pop(omitted["id"]),
    )
    name = report["generated_sources"][omitted["module"]]
    path = output / name
    tree = ast.parse(path.read_text())
    tree.body = [
        node
        for node in tree.body
        if not isinstance(node, ast.FunctionDef)
        or node.name != omitted["id"].split(".")[-1]
    ]
    path.write_text(ast.unparse(tree))
    report["raw"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        validate(bundle)


def test_generated_mutant_body_must_match_pinned_tool_and_current_source(bundle):
    _, output, report = bundle
    name = report["generated_sources"][MODULES[0]]
    path = output / name
    path.write_text(path.read_text().replace("value", "forged"))
    report["raw"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        validate(bundle)


@pytest.mark.parametrize("name", ["raw/baseline.json", "raw/observed-0.json"])
@pytest.mark.parametrize(
    "change", ["missing_phases", "hidden_collection_error", "hidden_setup_error"]
)
def test_execution_counts_must_agree_with_raw_pytest_phases(bundle, name, change):
    def alter(proof):
        if change == "missing_phases":
            proof["reports"] = []
        elif change == "hidden_collection_error":
            proof["collection_errors"] = ["SyntaxError during collection"]
        else:
            proof.setdefault("reports", []).append(
                {
                    "nodeid": "tests/test_critical.py::test_case",
                    "when": "setup",
                    "outcome": "failed",
                    "longrepr": "RuntimeError: fixture setup failed",
                    "duration": 0.01,
                }
            )

    rewrite_raw(bundle, name, alter)
    with pytest.raises(ValueError):
        validate(bundle)


def test_validate_cli_obeys_revision_threshold_and_age_policy(
    bundle, monkeypatch, capsys
):
    import sys

    root, output, _ = bundle
    monkeypatch.setattr(
        mutation,
        "git",
        lambda _root, *args: REVISION if args == ("rev-parse", "HEAD") else "",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mutation",
            "validate",
            "--root",
            str(root),
            "--output",
            str(output),
            "--revision",
            REVISION,
            "--threshold",
            "0.8",
            "--max-age-days",
            "30",
        ],
    )
    mutation.main()
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "killed": 8,
        "total": 10,
        "kill_rate": 0.8,
        "development_only": False,
    }


@pytest.mark.parametrize(
    "option,value",
    [
        ("--revision", "f" * 40),
        ("--threshold", "0.79"),
        ("--threshold", "nan"),
        ("--max-age-days", "0"),
    ],
)
def test_validate_cli_rejects_revision_mismatch_and_weakened_policy(
    bundle, monkeypatch, option, value
):
    import sys

    root, output, _ = bundle
    monkeypatch.setattr(
        mutation,
        "git",
        lambda _root, *args: REVISION if args == ("rev-parse", "HEAD") else "",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mutation",
            "validate",
            "--root",
            str(root),
            "--output",
            str(output),
            option,
            value,
        ],
    )
    with pytest.raises(SystemExit) as error:
        mutation.main()
    assert error.value.code == 1


def test_validate_cli_rejects_dirty_ci_tree(bundle, monkeypatch):
    import sys

    root, output, _ = bundle
    monkeypatch.setattr(
        mutation,
        "git",
        lambda _root, *args: REVISION if args == ("rev-parse", "HEAD") else " M app.py",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mutation",
            "validate",
            "--root",
            str(root),
            "--output",
            str(output),
            "--revision",
            REVISION,
        ],
    )
    with pytest.raises(SystemExit) as error:
        mutation.main()
    assert error.value.code == 1
