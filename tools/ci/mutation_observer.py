"""Pytest plugin retaining phase-aware results for mutmut's forked workers."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any
import uuid

import pytest

_state: dict[str, Any] = {}


def pytest_sessionstart(session: pytest.Session) -> None:
    _state.clear()
    _state.update(
        mutant=os.getenv("MUTANT_UNDER_TEST", ""),
        run_id=os.environ["KLUKAI_MUTATION_RUN_ID"],
        started_at=datetime.now(UTC).isoformat(),
        argv=list(session.config.invocation_params.args),
        reports=[],
        collection_errors=[],
        internal_errors=[],
    )


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    _state["reports"].append(
        {
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "longrepr": str(report.longrepr) if report.failed else "",
            "duration": report.duration,
        }
    )


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if report.failed:
        _state["collection_errors"].append(str(report.longrepr))


def pytest_internalerror(excrepr: object) -> None:
    _state["internal_errors"].append(str(excrepr))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    reports = _state["reports"]
    errors = len(_state["collection_errors"]) + len(_state["internal_errors"])
    errors += sum(r["outcome"] == "failed" and r["when"] != "call" for r in reports)
    calls = [
        r
        for r in reports
        if r["when"] == "call" and r["outcome"] in ("passed", "failed")
    ]
    _state.update(
        exit_code=int(exitstatus),
        collected=session.testscollected,
        calls=len(calls),
        failed_calls=sum(r["outcome"] == "failed" for r in calls),
        errors=errors,
        finished_at=datetime.now(UTC).isoformat(),
    )
    directory = Path(os.environ["KLUKAI_MUTATION_OBSERVATIONS"])
    directory.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(_state["mutant"].encode()).hexdigest()[:20]
    name = f"{identity}-{os.getpid()}-{uuid.uuid4().hex}.json"
    (directory / name).write_text(json.dumps(_state, indent=2))
