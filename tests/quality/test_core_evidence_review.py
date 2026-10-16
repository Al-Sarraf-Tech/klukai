"""Review regressions for authoritative streams, full lifecycle and raw arcs."""

from __future__ import annotations

from contextlib import closing
import json
import sqlite3

import pytest

from core_evidence_fixture import rehash
from test_core_evidence import full_bundle as full_bundle, observed as observed
from tools.ci.core_evidence import InvalidEvidence, validate
from tools.ci.core_profile import coverage_database
from tools.ci.core_source import sha256


def reconcile_observations(output):
    path = output / "results/validation.json"
    result = json.loads(path.read_text())
    result["retained_coverage"] = {
        **coverage_database(output / "results/.coverage"),
        "sha256": sha256(output / "results/.coverage"),
    }
    path.write_text(json.dumps(result))
    path = output / "manifest.json"
    report = json.loads(path.read_text())
    report["results"] = result
    path.write_text(json.dumps(report))
    rehash(output)


def replace_commands(output, rows):
    originals = {}
    for row in rows:
        for stream in ["stdout", "stderr"]:
            originals.setdefault(row[stream], (output / row[stream]).read_text())
    for path in output.glob("docker-*.*"):
        if path.name != "docker-commands.json":
            path.unlink()
    for index, row in enumerate(rows):
        for stream in ["stdout", "stderr"]:
            text = originals[row[stream]]
            row[stream] = f"docker-{index:03d}.{stream}"
            (output / row[stream]).write_text(text)
    (output / "docker-commands.json").write_text(json.dumps(rows))
    rehash(output)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_actual_attach_warning_invalidates_full_gate(full_bundle, stream):
    root, output, revision = full_bundle
    rows = json.loads((output / "docker-commands.json").read_text())
    row = next(r for r in rows if r["argv"][1:3] == ["start", "--attach"])
    (output / row[stream]).write_text(
        "ResourceWarning: unclosed database in producer shutdown\n"
    )
    rehash(output)
    with pytest.raises(InvalidEvidence, match="warning"):
        validate(root, output, revision)


@pytest.mark.parametrize("where", ["after-removal", "before-removal"])
def test_extra_successful_exec_is_not_producer_execution(full_bundle, where):
    root, output, revision = full_bundle
    rows = json.loads((output / "docker-commands.json").read_text())
    core = json.loads((output / "resources.json").read_text())["containers"][1]["id"]
    row = dict(rows[0])
    row["argv"] = [
        "docker",
        "exec",
        core,
        "/opt/ci/bin/python",
        "-c",
        "raise SystemExit(0)",
    ]
    index = (
        len(rows)
        if where == "after-removal"
        else next(
            i for i, r in enumerate(rows) if r["argv"][1:3] == ["start", "--attach"]
        )
        + 1
    )
    rows.insert(index, row)
    replace_commands(output, rows)
    with pytest.raises(InvalidEvidence, match="sequence|command"):
        validate(root, output, revision)


def test_impossible_nonbranch_arc_cannot_hide_in_lossy_json(full_bundle):
    root, output, revision = full_bundle
    with closing(sqlite3.connect(output / "results/.coverage")) as db:
        file_id = db.execute(
            "SELECT id FROM file WHERE path LIKE '%/app/domain.py'"
        ).fetchone()[0]
        context_id = db.execute("SELECT id FROM context LIMIT 1").fetchone()[0]
        db.execute(
            "INSERT INTO arc(file_id,context_id,fromno,tono) VALUES (?,?,3,4)",
            (file_id, context_id),
        )
        db.commit()
    reconcile_observations(output)
    with pytest.raises(InvalidEvidence, match="arc|control"):
        validate(root, output, revision)


@pytest.mark.parametrize(
    "kind",
    ["exception", "finally", "generator", "async", "generator-expression", "multiline"],
)
def test_real_runtime_arc_semantics(tmp_path, kind):
    import subprocess
    import sys
    from coverage import CoverageData
    from coverage.parser import PythonParser
    from tools.ci import core_results

    snippets = {
        "exception": (
            "def exercise(value):\n    try:\n        return 1/value\n    except ZeroDivisionError:\n        return 0\n",
            "exercise(0);exercise(1)",
        ),
        "finally": (
            "def exercise(value):\n    try:\n        if value:\n            return 1\n        raise ValueError()\n    finally:\n        marker=1\n",
            "exercise(True)\ntry: exercise(False)\nexcept ValueError: pass",
        ),
        "generator": (
            "def exercise():\n    try:\n        yield 1\n        yield 2\n    finally:\n        marker=1\n",
            "list(exercise())\ng=exercise();next(g);g.close()",
        ),
        "generator-expression": (
            "def exercise(value):\n    return all(\n        item > 0 for item in value\n    )\n",
            "exercise([1,2]);exercise([1,-1]);exercise([])",
        ),
        "multiline": (
            "def exercise(value):\n    if (\n        not value\n        or len(value) < 2\n        or any(\n            item < 0 for item in value\n        )\n    ):\n        return {item: item+1 for item in value}\n    return {}\n",
            "exercise([]);exercise([1]);exercise([1,2]);exercise([1,-1])",
        ),
        "async": (
            "import asyncio\nasync def child(value):\n    await asyncio.sleep(0)\n    if value:\n        raise ValueError()\n    return 2\nasync def exercise(value):\n    try:\n        return await child(value)\n    except ValueError:\n        return 0\n",
            "import asyncio\nasyncio.run(exercise(False));asyncio.run(exercise(True))",
        ),
    }
    source, call = snippets[kind]
    (tmp_path / "app").mkdir()
    (tmp_path / "app/__init__.py").touch()
    path = tmp_path / "app/scenarios.py"
    path.write_text(source)
    (tmp_path / "run.py").write_text(
        "import sys,pathlib;sys.path.insert(0,str(pathlib.Path(__file__).parent))\nfrom app.scenarios import exercise\n"
        + call
        + "\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-Werror",
            "-m",
            "coverage",
            "run",
            "--branch",
            "--source=app",
            "run.py",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = CoverageData(basename=str(tmp_path / ".coverage"))
    try:
        data.read()
        arcs = data.arcs(str(path))
        parser = PythonParser(filename=str(path))
        parser.parse_source()
        print(kind, "raw_arcs=", arcs, "outside_parser=", set(arcs) - parser.arcs())
        core_results.raw_arc_results(path, arcs)
    finally:
        data.close(force=True)


@pytest.mark.parametrize(
    "variant", ["stopped", "failed-stop", "timeout-stop", "os-error-stop"]
)
def test_exact_supported_postgres_cleanup_recovery(full_bundle, variant):
    root, output, revision = full_bundle
    rows = json.loads((output / "docker-commands.json").read_text())
    resources = json.loads((output / "resources.json").read_text())
    pg = resources["containers"][0]
    cid = pg["id"]
    waited = next(i for i, r in enumerate(rows) if r["argv"] == ["docker", "wait", cid])
    owned = rows[waited - 1]
    assert owned["argv"][-1] == pg["name"]
    for row in rows[:waited]:
        if row["argv"][1] == "inspect" and row["argv"][-1] in [cid, pg["name"]]:
            p = output / row["stdout"]
            info = json.loads(p.read_text())
            info["State"]["Running"] = True
            p.write_text(json.dumps(info))
    stopped = dict(next(r for r in rows if r["argv"] == ["docker", "start", cid]))
    stopped.update(
        argv=["docker", "stop", "--time=3", cid],
        timeout_seconds=15,
        exit_code=0 if variant == "stopped" else 1,
    )
    inserted = [stopped]
    if variant != "stopped":
        if variant in ["timeout-stop", "os-error-stop"]:
            stopped.update(
                exit_code=None,
                error_type="TimeoutExpired"
                if variant == "timeout-stop"
                else "PermissionError",
            )
        killed = dict(stopped)
        killed.pop("error_type", None)
        killed.update(argv=["docker", "kill", cid], timeout_seconds=30, exit_code=0)
        inserted.append(killed)
        (output / rows[waited]["stdout"]).write_text("137\n")
        p = output / rows[waited + 1]["stdout"]
        info = json.loads(p.read_text())
        info["State"]["ExitCode"] = 137
        p.write_text(json.dumps(info))
        pg.update(wait_exit_code=137, actual_exit_code=137)
        (output / "resources.json").write_text(json.dumps(resources))
    rows[waited:waited] = inserted
    replace_commands(output, rows)
    assert validate(root, output, revision)["certifying"] is True


@pytest.mark.parametrize(
    "variant", ["attach-exception", "extra-inspect", "extra-network", "reorder-exec"]
)
def test_command_recovery_is_only_permitted_in_its_exact_phase(full_bundle, variant):
    root, output, revision = full_bundle
    rows = json.loads((output / "docker-commands.json").read_text())
    if variant == "attach-exception":
        row = next(r for r in rows if r["argv"][1:3] == ["start", "--attach"])
        row.update(exit_code=None, error_type="TimeoutExpired")
    elif variant == "reorder-exec":
        selected = [
            i
            for i, r in enumerate(rows)
            if r["argv"][1] == "exec" and ("psql" in r["argv"] or "id" in r["argv"])
        ]
        rows[selected[0]], rows[selected[1]] = rows[selected[1]], rows[selected[0]]
    else:
        selected = next(
            r
            for r in rows
            if r["argv"][1] == ("inspect" if variant == "extra-inspect" else "network")
        )
        rows.append(dict(selected))
    replace_commands(output, rows)
    with pytest.raises(InvalidEvidence):
        validate(root, output, revision)


@pytest.mark.parametrize("failures", [2, 199, 200])
def test_readiness_repetitions_have_exact_order_and_bound(full_bundle, failures):
    from datetime import datetime, timedelta

    root, output, revision = full_bundle
    rows = json.loads((output / "docker-commands.json").read_text())
    index = next(i for i, r in enumerate(rows) if "pg_isready" in r["argv"])
    failed = dict(rows[index])
    failed["exit_code"] = 1
    rows[index:index] = [dict(failed) for _ in range(failures)]
    p = output / "manifest.json"
    report = json.loads(p.read_text())
    report["started_at"] = (
        datetime.fromisoformat(report["started_at"]) - timedelta(seconds=0.3 * failures)
    ).isoformat()
    p.write_text(json.dumps(report))
    replace_commands(output, rows)
    if failures < 200:
        assert validate(root, output, revision)["certifying"] is True
    else:
        with pytest.raises(InvalidEvidence, match="readiness|sequence"):
            validate(root, output, revision)


@pytest.mark.parametrize("role", ["core", "postgres"])
def test_exact_already_absent_cleanup_is_consumed(full_bundle, role):
    root, output, revision = full_bundle
    rows = json.loads((output / "docker-commands.json").read_text())
    resources = json.loads((output / "resources.json").read_text())
    resource = next(r for r in resources["containers"] if r["role"] == role)
    selected = [
        i
        for i, r in enumerate(rows)
        if r["argv"][1] == "inspect" and r["argv"][-1] == resource["name"]
    ]
    absent = dict(rows[selected[-1]])
    rows[selected[0] : selected[-1] + 1] = [absent]
    for key in ["wait_exit_code", "actual_exit_code"]:
        resource.pop(key)
    (output / "resources.json").write_text(json.dumps(resources))
    replace_commands(output, rows)
    assert validate(root, output, revision)["certifying"] is True
