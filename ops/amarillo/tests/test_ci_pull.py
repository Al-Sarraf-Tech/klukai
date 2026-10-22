"""amarillo pulls Dominus's CI records; it never deletes or overwrites its own."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "klukai-ci-pull.py"
SPEC = importlib.util.spec_from_file_location("klukai_ci_pull", SCRIPT)
pull = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pull
SPEC.loader.exec_module(pull)

SHA = "a" * 40


def done(stdout="", rc=0):
    return lambda argv, **kw: subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="")


def test_only_recent_well_formed_run_records_are_listed():
    listing = f"{SHA}/39-1\n{SHA}/40-2\ncache/pip\n../etc\n{SHA}/not-a-run\n"
    assert pull.recent(done(listing)) == [f"{SHA}/39-1", f"{SHA}/40-2"]


def test_an_unreachable_dominus_is_not_an_error():
    assert pull.recent(done(rc=255)) is None


def test_rsync_copies_only_the_listed_records_without_deleting(tmp_path):
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw.get("input")))
        return subprocess.CompletedProcess(argv, 0, "", "")

    pull.pull([f"{SHA}/39-1"], run, tmp_path)
    assert len(seen) == 2
    for argv, given in seen:
        rsync = argv[argv.index("rsync"):]
        assert "--files-from=-" in rsync and "--safe-links" in rsync
        assert not any(a.startswith("--delete") or a == "--remove-source-files" for a in rsync)
        assert given == f"{SHA}/39-1\n" and rsync[-1] == f"{tmp_path}/"


def test_the_deployer_s_frontend_evidence_goes_first_and_the_rest_is_time_boxed(tmp_path):
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "", "")

    pull.pull([f"{SHA}/39-1"], run, tmp_path)
    (first, _), (rest, kw) = seen
    assert first[0] == "rsync" and "--include=/*/*/frontend/***" in first and "--exclude=/*/*/*" in first
    assert rest[0] == "timeout" and str(pull.BULK_SECONDS) in rest
    assert not any(a.startswith("--include") or a.startswith("--exclude") for a in rest)
    assert kw["timeout"] > pull.BULK_SECONDS


def test_a_time_boxed_bulk_copy_that_runs_out_of_time_is_not_an_error(tmp_path):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 124 if argv[0] == "timeout" else 0, "", "")

    pull.pull([f"{SHA}/39-1"], run, tmp_path)


def test_a_failed_copy_is_reported(tmp_path):
    import pytest

    with pytest.raises(RuntimeError, match="exit 12"):
        pull.pull([f"{SHA}/39-1"], done(rc=12), tmp_path)


def test_nothing_recent_means_no_copy():
    calls = []
    pull.pull([], lambda *a, **k: calls.append(a))
    assert calls == []


def test_a_real_copy_merges_into_the_local_tree_and_keeps_amarillo_files(tmp_path):
    remote, local = tmp_path / "remote", tmp_path / "local"
    (remote / SHA / "39-1" / "frontend").mkdir(parents=True)
    (remote / SHA / "39-1" / "frontend" / "manifest.json").write_text("dominus")
    (local / SHA / "39-1" / "release").mkdir(parents=True)
    (local / SHA / "39-1" / "release" / "manifest.json").write_text("amarillo")

    def run(argv, **kw):
        argv = argv[argv.index("rsync"):]  # run it now, without the time box
        argv = [a for a in argv if not a.startswith("--rsync-path")]
        argv = argv[:argv.index("-e")] + argv[argv.index("-e") + 2:]
        argv[-2] = f"{remote}/"
        return subprocess.run(argv, input=kw["input"], capture_output=True, text=True)

    pull.pull([f"{SHA}/39-1"], run, local)
    assert (local / SHA / "39-1" / "frontend" / "manifest.json").read_text() == "dominus"
    assert (local / SHA / "39-1" / "release" / "manifest.json").read_text() == "amarillo"
    assert (local / SHA / "39-1" / "frontend" / "manifest.json").stat().st_mode & 0o777 == 0o600


def test_the_priority_pass_alone_copies_only_frontend_job_directories(tmp_path):
    remote, local = tmp_path / "remote", tmp_path / "local"
    for job in ("frontend", "ci-image"):
        (remote / SHA / "39-1" / job / "pwa").mkdir(parents=True)
        (remote / SHA / "39-1" / job / "pwa" / "main.js").write_text(job)
    calls = []

    def run(argv, **kw):
        calls.append(argv[0])
        if argv[0] == "timeout":  # the bulk pass "ran out of time" before copying anything
            return subprocess.CompletedProcess(argv, 124, "", "")
        argv = [a for a in argv if not a.startswith("--rsync-path")]
        argv = argv[:argv.index("-e")] + argv[argv.index("-e") + 2:]
        argv[-2] = f"{remote}/"
        return subprocess.run(argv, input=kw["input"], capture_output=True, text=True)

    pull.pull([f"{SHA}/39-1"], run, local)
    assert calls == ["rsync", "timeout"]
    assert (local / SHA / "39-1" / "frontend" / "pwa" / "main.js").read_text() == "frontend"
    assert not (local / SHA / "39-1" / "ci-image").exists()
