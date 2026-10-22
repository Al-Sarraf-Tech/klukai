"""The CI-built PWA is relayed only when every byte matches the run's own log."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ops/amarillo/scripts/klukai-fetch-ci-artifacts.py"
SPEC = importlib.util.spec_from_file_location("klukai_fetch_ci_artifacts_web", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
relay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = relay
SPEC.loader.exec_module(relay)
from tools.ci.frontend_evidence import inventory  # noqa: E402

REV = "c6d02f0c884cd4585aaab96c28a13c32bbb6fc6f"
RUN = "9-1"
BOOTSTRAP = '{"mainJsPath":"main.dart.js"};c("main.dart.js");\n'
INDEX = '<script src="flutter_bootstrap.js"></script>\n'


def ci_artifacts(root: Path, revision: str = REV, run: str = RUN, **tamper: bytes) -> dict:
    """A frontend evidence directory as the CI frontend job leaves it."""
    pwa = root / "artifacts" / revision / run / "frontend" / "pwa"
    files = {
        "index.html": INDEX.encode(),
        "flutter_bootstrap.js": BOOTSTRAP.encode(),
        "main.dart.js": b"void main(){}\n",
        "login.html": b"<html>login</html>\n",
        "canvaskit/canvaskit.wasm": b"\0asm",
    }
    for name, body in files.items():
        (pwa / name).parent.mkdir(parents=True, exist_ok=True)
        (pwa / name).write_bytes(body)
    listed = inventory(pwa)
    for name, body in tamper.items():
        (pwa / name).write_bytes(body)
    (pwa.parent / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "source": {"revision": revision}, "pwa": listed})
    )
    return listed


def guest(root: Path) -> object:
    return relay.LocalGuest({relay.ARTIFACTS: root / "artifacts"})


def test_relays_the_exact_ci_tree_and_builds_the_hashed_release(tmp_path):
    listed = ci_artifacts(tmp_path)
    out = relay.fetch_web(guest(tmp_path), RUN, REV, tmp_path / "web", pwa_sha256=listed["sha256"])
    release = tmp_path / "web" / "release"
    assert out["ci_pwa_sha256"] == listed["sha256"]
    assert out["release_sha256"] == inventory(release)["sha256"]
    assert (release / out["entry"]["main"]).is_file()
    assert out["entry"]["bootstrap"] in (release / "index.html").read_text()
    assert not (tmp_path / "web" / "ci-pwa").exists()


def test_a_digest_the_job_did_not_log_is_refused(tmp_path):
    ci_artifacts(tmp_path)
    with pytest.raises(ValueError, match="logged"):
        relay.fetch_web(guest(tmp_path), RUN, REV, tmp_path / "web", pwa_sha256="0" * 64)


def test_a_file_changed_on_the_shared_disk_after_the_manifest_is_refused(tmp_path):
    listed = ci_artifacts(tmp_path, **{"main.dart.js": b"evil();\n"})
    with pytest.raises(ValueError, match="main.dart.js"):
        relay.fetch_web(guest(tmp_path), RUN, REV, tmp_path / "web", pwa_sha256=listed["sha256"])


def test_a_manifest_for_another_revision_is_refused(tmp_path):
    listed = ci_artifacts(tmp_path, revision=REV)
    manifest = tmp_path / "artifacts" / REV / RUN / "frontend" / "manifest.json"
    doc = json.loads(manifest.read_text())
    doc["source"]["revision"] = "d" * 40
    manifest.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="different revision"):
        relay.fetch_web(guest(tmp_path), RUN, REV, tmp_path / "web", pwa_sha256=listed["sha256"])


@pytest.mark.parametrize("run", ["9", "../9-1", "9-1/../../x"])
def test_run_identity_cannot_escape_the_artifact_tree(tmp_path, run):
    with pytest.raises(ValueError):
        relay.fetch_web(guest(tmp_path), run, REV, tmp_path / "web", pwa_sha256="a" * 64)


def test_a_path_traversal_entry_in_the_manifest_is_refused(tmp_path):
    listed = ci_artifacts(tmp_path)
    manifest = tmp_path / "artifacts" / REV / RUN / "frontend" / "manifest.json"
    files = dict(listed["files"])
    files["../../escape"] = "a" * 64
    manifest.write_text(json.dumps({"schema_version": 1, "source": {"revision": REV},
                                    "pwa": {"files": files, "sha256": listed["sha256"]}}))
    with pytest.raises(ValueError):
        relay.fetch_web(guest(tmp_path), RUN, REV, tmp_path / "web", pwa_sha256=listed["sha256"])
    assert not (tmp_path / "escape").exists()


def test_web_provenance_is_signed_and_verifies(tmp_path):
    listed = ci_artifacts(tmp_path)
    key = tmp_path / "key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(key)], check=True)
    signers = tmp_path / "allowed_signers"
    signers.write_text("klukai-release@amarillo namespaces=\"klukai-release\" " + (tmp_path / "key.pub").read_text())
    out = relay.fetch_web(guest(tmp_path), RUN, REV, tmp_path / "web", pwa_sha256=listed["sha256"], key=key)
    provenance = tmp_path / "web" / "web-provenance.json"
    doc = json.loads(provenance.read_text())
    assert doc["kind"] == "klukai-web" and doc["revision"] == REV and doc["ci_run"] == RUN
    assert doc["release"] == inventory(tmp_path / "web" / "release")
    with provenance.open("rb") as data:
        verified = subprocess.run(
            ["ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", "klukai-release@amarillo",
             "-n", "klukai-release", "-s", out["signature"]],
            stdin=data, capture_output=True,
        )
    assert verified.returncode == 0, verified.stderr


def jobs_api(conclusion: str = "success"):
    def api(path: str, token: str) -> dict:
        assert "/runs/9/attempts/1/jobs" in path
        common = {"run_id": 9, "head_sha": REV, "status": "completed"}
        return {"total_count": 2,
                "jobs": [{**common, "id": 4, "name": "Lint", "conclusion": "success"},
                         {**common, "id": 8, "name": relay.FRONTEND_JOB, "conclusion": conclusion}]}
    return api


def test_the_pwa_digest_comes_from_the_runs_own_frontend_job():
    digest = hashlib.sha256(b"x").hexdigest()
    seen = {}

    def log(repo, job_id, token):
        seen["job"] = job_id
        return f'python3 -c ... "$verified"\n{{"pwa_sha256": "{digest}", "revision": "{REV}"}}\nKLUKAI_PWA_SHA256={digest}\n'

    assert relay.ci_pwa_digest("o/r", RUN, "tok", jobs_api(), log, revision=REV) == digest
    assert seen["job"] == 8


@pytest.mark.parametrize("text", ["nothing here", f"KLUKAI_PWA_SHA256={'a' * 64}\nKLUKAI_PWA_SHA256={'b' * 64}\n"])
def test_a_missing_or_ambiguous_pwa_digest_is_refused(text):
    with pytest.raises(ValueError):
        relay.ci_pwa_digest("o/r", RUN, "tok", jobs_api(), lambda *a: text, revision=REV)


def test_a_failed_frontend_job_cannot_supply_a_digest():
    with pytest.raises(ValueError):
        relay.ci_pwa_digest("o/r", RUN, "tok", jobs_api("failure"),
                            lambda *a: f"KLUKAI_PWA_SHA256={'a' * 64}", revision=REV)


class FlakySearch:
    """GitHub's commit search on 2026-10-22: identical queries returned 0, 2, 0 runs."""

    def __init__(self, run, answers):
        self.run, self.answers, self.calls = run, list(answers), []

    def __call__(self, path, token):
        self.calls.append(path)
        if "/jobs" in path:
            return {"total_count": 0, "jobs": []}
        found = self.answers.pop(0) if self.answers else False
        return {"workflow_runs": [self.run] if found else []}


def test_a_run_missing_from_one_search_is_found_by_another():
    run = {"id": 7, "head_sha": REV, "status": "completed", "conclusion": "success", "run_attempt": 1}
    search = FlakySearch(run, [False, True])  # filtered search empty, unfiltered has it
    assert [r["id"] for r in relay.runs_for("o/r", REV, "tok", search)] == [7]


def test_an_empty_search_is_retried_and_the_branch_listing_is_consulted():
    run = {"id": 9, "head_sha": REV, "status": "completed", "conclusion": "success", "run_attempt": 1}
    search = FlakySearch(run, [False, False, False, False, False, True])  # second pass, branch listing
    found = relay.runs_for("o/r", REV, "tok", search, branch="main")
    assert [r["id"] for r in found] == [9]
    assert any("branch=main" in c for c in search.calls)


def test_runs_of_other_commits_in_the_branch_listing_are_ignored():
    other = {"id": 3, "head_sha": "d" * 40, "status": "completed", "conclusion": "success", "run_attempt": 1}
    search = FlakySearch(other, [True] * 9)
    assert relay.runs_for("o/r", REV, "tok", search, branch="main") == []
