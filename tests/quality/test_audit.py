"""Adversarial audit fixtures must never receive a passing certification."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def executable(path: Path, source: str) -> None:
    path.write_text(source)
    path.chmod(0o755)


@pytest.fixture
def audit_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    for directory in ("scripts", "docker/core/app", "docs", ".github/workflows"):
        (repo / directory).mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "scripts/s-tier-audit.sh", repo / "scripts/s-tier-audit.sh")
    if (ROOT / "tools/quality").exists():
        shutil.copytree(ROOT / "tools/quality", repo / "tools/quality")
    (repo / "docker/core/app/example.py").write_text(
        "def answer() -> int:\n    return 42\n"
    )
    (repo / "docker/core/requirements.txt").write_text(
        "example==1.0 --hash=sha256:fixture\n"
    )
    (repo / "docker/core/.coveragerc").write_text("[run]\nbranch=True\n")
    (repo / "docker/core/mypy.ini").write_text("[mypy]\nstrict = True\n")
    (repo / "docker/core/Dockerfile").write_text(
        "# Future: distroless runtime\nFROM python:3.14-slim\nUSER appuser\n"
    )
    (repo / "docs/mutation-results.json").write_text(
        json.dumps({"_baseline_run_at": "2026-05-17T13:15:00-05:00", "results": []})
    )
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    for tool in ("ruff", "mypy", "bandit", "pip-audit"):
        executable(bin_path / tool, "#!/bin/sh\nexit 0\n")
    executable(
        bin_path / "radon",
        '#!/bin/sh\ncase " $* " in\n'
        '*\' -j \'*|*\' --json \'*) echo \'{"app/example.py": [{"type": "function", "name": "answer", "complexity": 12}]}\' ;;\n'
        "*) echo 'Average complexity: C (12.0)' ;;\nesac\n",
    )
    # Legacy audit coverage subprocesses are irrelevant to these negative
    # fixtures. Forward all other invocations to the real Python interpreter.
    python = bin_path / "audit-python"
    executable(
        python,
        f"#!{sys.executable}\nimport os,sys\n"
        "if sys.argv[1:3] == ['-m', 'pytest']:\n"
        " print('TOTAL 1 0 100%')\n"
        " print('Required test coverage of 95% reached.')\n"
        "else:\n"
        f" os.execv({sys.executable!r}, [{sys.executable!r}] + sys.argv[1:])\n",
    )
    env = dict(os.environ, PATH=f"{bin_path}:/usr/bin:/bin", PYTHON=str(python))
    env.pop("KLUKAI_AUDIT_EVIDENCE_DIR", None)
    return repo, env


def report(repo: Path, env: dict[str, str], dimension: str) -> dict:
    result = subprocess.run(
        [
            "bash",
            str(repo / "scripts/s-tier-audit.sh"),
            f"--only={dimension}",
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode in (0, 1), result.stderr
    return json.loads(result.stdout)


def matching_check(result: dict, word: str) -> dict:
    matches = [check for check in result["checks"] if word in check["name"].lower()]
    assert len(matches) == 1, matches
    return matches[0]


def test_unexamined_dimensions_never_receive_s(audit_repo):
    """A code-only run says nothing about security or deployment."""
    repo, env = audit_repo
    result = report(repo, env, "code")
    assert result["dimensions"]["security"]["tier"] not in ("S", "S+")
    assert result["tier"] not in ("S", "S+")


def test_complexity_uses_numeric_result_not_successful_tool_exit(audit_repo):
    repo, env = audit_repo
    result = report(repo, env, "code")
    assert matching_check(result, "complexity")["status"] != "✓"


def test_distroless_comment_cannot_certify_slim_runtime(audit_repo):
    repo, env = audit_repo
    result = report(repo, env, "security")
    assert matching_check(result, "distroless")["status"] != "✓"


def test_fresh_mtime_cannot_certify_old_empty_mutation_results(audit_repo):
    repo, env = audit_repo
    result = report(repo, env, "testing")
    assert matching_check(result, "mutation")["status"] != "✓"


def test_missing_secret_scanner_fails_instead_of_informational_success(audit_repo):
    repo, env = audit_repo
    result = report(repo, env, "security")
    assert matching_check(result, "gitleaks")["status"] != "✓"
