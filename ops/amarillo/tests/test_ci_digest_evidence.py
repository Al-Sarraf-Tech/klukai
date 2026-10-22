"""Digest consumers must recheck exact, complete CI evidence before reading logs.

Only in-memory fake APIs/logs are called. No controller/relay CLI, network,
artifact transport, host inspection, or real signing operation is involved.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "klukai_digest_evidence_tests", ROOT / "ops/amarillo/scripts/klukai-fetch-ci-artifacts.py"
)
assert SPEC is not None and SPEC.loader is not None
relay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = relay
SPEC.loader.exec_module(relay)

REV = "c6d02f0c884cd4585aaab96c28a13c32bbb6fc6f"
DIGEST = "a" * 64
RUN = {"id": 9, "run_attempt": 1, "head_sha": REV,
       "status": "completed", "conclusion": "success"}
KINDS = [("ci_manifest_digest", relay.SCAN_JOB, "KLUKAI_RELEASE_MANIFEST_SHA256"),
         ("ci_pwa_digest", relay.FRONTEND_JOB, "KLUKAI_PWA_SHA256")]


def job(identity: int, name: str) -> dict:
    return {"id": identity, "run_id": 9, "head_sha": REV, "run_attempt": 1,
            "name": name, "status": "completed", "conclusion": "success"}


class Evidence:
    def __init__(self, kind):
        self.function, self.name, self.marker = kind
        self.jobs = [job(i, name) for i, name in enumerate(sorted(relay.FULL_CI_GATES), 1)]
        self.reported_total = None
        self.pages = []
        self.logs = []

    def api(self, path, token):
        assert token == "fake-token"
        if "/workflows/ci.yml/runs?" in path:
            return {"workflow_runs": [copy.deepcopy(RUN)]}
        parts = urlsplit(path)
        assert parts.path == "/repos/o/r/actions/runs/9/attempts/1/jobs"
        params = parse_qs(parts.query)
        size = int(params["per_page"][0])
        page = int(params.get("page", ["1"])[0])
        self.pages.append(page)
        start = (page - 1) * size
        total = len(self.jobs) if self.reported_total is None else self.reported_total
        return {"total_count": total, "jobs": copy.deepcopy(self.jobs[start:start + size])}

    def log(self, repo, identity, token):
        assert repo == "o/r" and token == "fake-token"
        self.logs.append(identity)
        return f"{self.marker}={DIGEST}\n"

    def certify(self):
        assert relay.green_run("o/r", REV, "fake-token", self.api) == "9-1"
        self.pages.clear()

    def read(self):
        return getattr(relay, self.function)("o/r", "9-1", "fake-token", self.api, self.log,
                                             revision=REV)

    def target(self):
        return next(item for item in self.jobs if item["name"] == self.name)


@pytest.fixture(params=KINDS, ids=["image", "pwa"])
def evidence(request):
    return Evidence(request.param)


def test_digest_job_on_second_page_is_found(evidence):
    target = evidence.target()
    evidence.jobs.remove(target)
    evidence.jobs = [job(i, f"extra-{i}") for i in range(1000, 1100)] + evidence.jobs + [target]
    evidence.certify()
    assert evidence.read() == DIGEST
    assert evidence.pages == [1, 2]
    assert evidence.logs == [target["id"]]


@pytest.mark.parametrize("duplicate", ["successful-name", "failed-name", "job-id"])
def test_digest_rejects_duplicate_evidence_beyond_first_page(evidence, duplicate):
    evidence.certify()
    duplicate_job = dict(evidence.target(), id=999)
    if duplicate == "failed-name":
        duplicate_job["conclusion"] = "failure"
    elif duplicate == "job-id":
        duplicate_job = dict(evidence.target(), name="other-job")
    evidence.jobs += [job(i, f"extra-{i}") for i in range(1000, 1100)] + [duplicate_job]
    with pytest.raises(ValueError):
        evidence.read()
    assert evidence.logs == []


@pytest.mark.parametrize("field, value", [
    ("run_id", 10), ("head_sha", "b" * 40), ("run_attempt", 2),
    ("status", "in_progress"), ("run_id", None), ("head_sha", None),
    ("run_id", 9.0), ("run_attempt", 1.0), ("run_attempt", True),
])
def test_digest_rejects_identity_or_status_changed_after_certification(evidence, field, value):
    evidence.certify()
    evidence.target()[field] = value
    with pytest.raises(ValueError):
        evidence.read()
    assert evidence.logs == []


def test_digest_rejects_an_incomplete_jobs_snapshot(evidence):
    evidence.certify()
    evidence.reported_total = len(evidence.jobs) + 1
    with pytest.raises(ValueError):
        evidence.read()
    assert evidence.logs == []


@pytest.mark.parametrize("included", [True, False], ids=["attempt-present", "attempt-absent"])
def test_digest_accepts_consistent_optional_attempt_metadata(evidence, included):
    evidence.certify()
    if not included:
        for item in evidence.jobs:
            item.pop("run_attempt")
    assert evidence.read() == DIGEST
    assert evidence.logs == [evidence.target()["id"]]


def test_unrelated_completed_skipped_job_does_not_disqualify_a_digest(evidence):
    evidence.jobs.append(dict(job(999, "unrelated-optional-job"), conclusion="skipped"))
    evidence.certify()
    assert evidence.read() == DIGEST


def isolated_function(path, name, namespace, *, owner=None):
    """Compile only the selected repository function with fake dependencies."""
    module = ast.parse(path.read_text())
    body = module.body
    if owner is not None:
        body = next(node.body for node in body if isinstance(node, ast.ClassDef) and node.name == owner)
    node = next(node for node in body if isinstance(node, ast.FunctionDef) and node.name == name)
    code = compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec")
    exec(code, namespace)  # noqa: S102 - only repository function AST, with fake dependencies
    return namespace[name]


@pytest.mark.parametrize("command", ["image", "web"])
def test_standalone_relay_passes_expected_revision_to_its_digest_consumer(command):
    seen = []

    def digest(repo, run, token, *, revision):
        assert (repo, run, token, revision) == ("o/r", "9-1", "fake-token", REV)
        seen.append(command)
        return DIGEST

    namespace = {"os": SimpleNamespace(environ={"GH_TOKEN": "fake-token"}),
                 "green_run": lambda *args: "9-1", "ci_manifest_digest": digest,
                 "ci_pwa_digest": digest, "fetch_image": lambda *args, **kwargs: {},
                 "fetch_web": lambda *args, **kwargs: {},
                 "json": SimpleNamespace(dumps=lambda *args, **kwargs: "fake-result")}
    execute = isolated_function(Path(relay.__file__), "_run", namespace)
    args = SimpleNamespace(command=command, signing_key=SimpleNamespace(is_file=lambda: True),
                           repo="o/r", revision=REV, dest=Path("unused"))
    assert execute(args, object()) == 0
    assert seen == [command]


def test_deploy_on_merge_passes_expected_revision_to_both_digest_consumers():
    seen = []

    def digest(repo, run, token, *args, revision):
        assert (repo, run, token, revision) == ("o/r", "9-1", "fake-token", REV)
        seen.append(revision)
        return DIGEST

    fake_relay = SimpleNamespace(ci_manifest_digest=digest, ci_pwa_digest=digest,
                                 fetch_image=lambda *args, **kwargs: {},
                                 fetch_web=lambda *args, **kwargs: {})
    path = ROOT / "ops/amarillo/scripts/klukai-deploy-on-merge.py"
    execute = isolated_function(path, "relay_artifacts", {"relay": fake_relay}, owner="Deployer")
    fake = SimpleNamespace(guest=lambda: object(), token="fake-token",
                           cfg=SimpleNamespace(repo="o/r", releases=Path("unused"), signing_key="unused"),
                           api_for_relay=object(), job_log=object())
    assert execute(fake, REV, "9-1", True) == {"core": {}, "web": {}}
    assert seen == [REV, REV]
