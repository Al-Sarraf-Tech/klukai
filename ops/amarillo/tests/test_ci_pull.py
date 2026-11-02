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


def test_records_are_listed_with_their_fingerprints():
    listing = (f"3:120:1792700000.5 {SHA}/39-1\n7:900:1792700100.0 {SHA}/40-2\n"
               "1:4:1792700000.0 cache/pip\n1:1:1.0 ../etc\n2:2:2.0 \n")
    assert pull.recent(done(listing)) == {f"{SHA}/39-1": "3:120:1792700000.5",
                                          f"{SHA}/40-2": "7:900:1792700100.0"}


def test_an_unreachable_dominus_is_not_an_error():
    assert pull.recent(done(rc=255)) is None


def test_only_changed_records_are_copied_oldest_first_and_never_after_archiving(tmp_path):
    local, archive = tmp_path / "local", tmp_path / "archive"
    (local / ("c" * 40)).mkdir(parents=True)
    archive.mkdir()
    (archive / f"{'b' * 40}.tar").write_text("archived")
    (archive / f"{'c' * 40}.tar").write_text("archived, but this revision has new local runs")
    listing = {f"{SHA}/2-1": "5:50:300.0", f"{SHA}/1-1": "5:50:100.0", f"{SHA}/3-1": "9:90:200.0",
               f"{'b' * 40}/4-1": "1:1:50.0", f"{'c' * 40}/5-1": "1:1:60.0"}
    synced = {f"{SHA}/3-1": "9:90:200.0", f"{SHA}/2-1": "4:40:250.0"}
    assert pull.todo(listing, synced, local, archive) == [f"{'c' * 40}/5-1", f"{SHA}/1-1", f"{SHA}/2-1"]


def test_a_complete_copy_is_remembered_and_a_cut_short_one_is_not(tmp_path):
    record = f"{SHA}/39-1"
    state = tmp_path / "state.json"
    listings = [f"3:120:100.0 {record}\n"]

    def run(argv, **kw):
        if argv[0] == "ssh":
            return subprocess.CompletedProcess(argv, 0, listings[0], "")
        return subprocess.CompletedProcess(argv, outcome[0] if argv[0] == "timeout" else 0, "", "")

    outcome = [124]
    pull.main(run, local=tmp_path, archive=tmp_path / "none", state=state)
    assert pull.load(state) == {}
    outcome = [0]
    pull.main(run, local=tmp_path, archive=tmp_path / "none", state=state)
    assert pull.load(state) == {record: "3:120:100.0"}
    calls = []
    pull.main(lambda argv, **kw: (calls.append(argv[0]), run(argv, **kw))[1],
              local=tmp_path, archive=tmp_path / "none", state=state)
    assert calls == ["ssh"]  # unchanged since its complete copy: no rsync at all
    listings[0] = ""  # Dominus pruned it: forget it
    pull.main(run, local=tmp_path, archive=tmp_path / "none", state=state)
    assert pull.load(state) == {}


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
        # Directories get local times, so the archiver sees a pull still in progress.
        assert "--omit-dir-times" in rsync


def test_the_deployer_s_frontend_evidence_goes_first_and_the_rest_is_time_boxed(tmp_path):
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "", "")

    pull.pull([f"{SHA}/39-1"], run, tmp_path)
    (first, _), (rest, kw) = seen
    assert first[0] == "rsync" and "--include=/*/*/frontend/***" in first and "--exclude=/*/*/*" in first
    # A job amarillo takes over from Dominus is waiting for this image.
    assert "--include=/*/*/ci-tool-image/***" in first
    assert first.index("--include=/*/*/ci-tool-image/***") < first.index("--exclude=/*/*/*")
    assert rest[0] == "timeout" and int(rest[rest.index("--kill-after=10") + 1]) <= pull.BULK_SECONDS
    assert not any(a.startswith("--include") or a.startswith("--exclude") for a in rest)
    assert kw["timeout"] > pull.BULK_SECONDS


def test_a_time_boxed_bulk_copy_that_runs_out_of_time_is_not_an_error(tmp_path):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 124 if argv[0] == "timeout" else 0, "", "")

    assert pull.pull([f"{SHA}/39-1"], run, tmp_path) == []


def test_records_are_copied_one_at_a_time_and_each_counts_once_complete(tmp_path):
    old, new = f"{SHA}/1-1", f"{SHA}/2-1"
    bulk, now = [], [0.0]

    def run(argv, **kw):
        if argv[0] != "timeout":
            return subprocess.CompletedProcess(argv, 0, "", "")
        bulk.append(kw["input"])
        now[0] += 30  # each copy takes 30 s of the 45 s box
        return subprocess.CompletedProcess(argv, 0 if len(bulk) == 1 else 124, "", "")

    assert pull.pull([old, new], run, tmp_path, clock=lambda: now[0]) == [old]
    assert bulk == [old + "\n", new + "\n"]  # oldest first, its own file list only
    bulk.clear()
    now[0] = 0.0

    def slow(argv, **kw):
        if argv[0] == "timeout":
            bulk.append(kw["input"])
            now[0] += 42
        return subprocess.CompletedProcess(argv, 0, "", "")

    # 3 s left after the first copy: too little to start the next one.
    assert pull.pull([old, new], slow, tmp_path, clock=lambda: now[0]) == [old]
    assert bulk == [old + "\n"]


def test_a_failed_copy_is_reported(tmp_path):
    import pytest

    with pytest.raises(RuntimeError, match="exit 12"):
        pull.pull([f"{SHA}/39-1"], done(rc=12), tmp_path)


def test_nothing_to_copy_means_no_rsync():
    calls = []
    assert pull.pull([], lambda *a, **k: calls.append(a)) == []
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
