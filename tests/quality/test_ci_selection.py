"""Affected gates are conservative and never certify a partial release."""

import pytest
from tools.ci import selection


@pytest.mark.parametrize(
    "paths,expected",
    [
        (["README.md"], set()),
        (["docs/handoff/example.md"], set()),
        (["flutter_app/lib/chat.dart"], {"frontend"}),
        (["web/index.html"], {"frontend"}),
        (["docker/core/app/chat.py"], {"core"}),
        (["docker/core/tests/test_chat.py"], {"core"}),
        (["ops/amarillo/tests/test_ci_runner.py"], {"components"}),
        (["docker/voice/app.py"], {"components"}),
        (["tools/avatar/test.mjs"], {"components"}),
        (["tgbot/bot.py"], set()),
        (
            ["docker/core/app/chat.py", "flutter_app/lib/chat.dart"],
            {"core", "frontend"},
        ),
    ],
)
def test_known_components_select_only_affected_gates(paths, expected):
    result = selection.plan(paths)
    assert result == {
        "full": False,
        **{key: key in expected for key in selection.GROUPS},
    }


@pytest.mark.parametrize(
    "path",
    [
        "unknown.py",
        ".github/workflows/ci.yml",
        "Makefile",
        "tools/ci/core_runner.py",
        "docker/core/requirements-ci.txt",
        "docker/core/Dockerfile",
        "scripts/ci/frontend.sh",
        "tests/quality/test_ci_selection.py",
        "../bad",
        "/absolute",
    ],
)
def test_unknown_shared_build_or_dependency_changes_run_every_gate(path):
    assert all(selection.plan([path]).values())


def test_dispatch_and_unavailable_diff_always_run_full():
    assert all(selection.plan(["README.md"], full=True).values())
    assert all(selection.plan(None).values())
    assert all(selection.plan([]).values())


def test_routine_result_requires_each_planned_gate_and_rejects_unplanned_execution():
    selected = selection.plan(["flutter_app/lib/chat.dart"])
    outcomes = {
        name: {
            "result": "success" if name in selection.required(selected) else "skipped"
        }
        for name in selection.JOBS
    }
    selection.check(selected, outcomes)
    outcomes["frontend"]["result"] = "failure"
    with pytest.raises(ValueError):
        selection.check(selected, outcomes)
    outcomes["frontend"]["result"] = "skipped"
    with pytest.raises(ValueError):
        selection.check(selected, outcomes)
