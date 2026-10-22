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
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, input=kw.get("input"))
        return subprocess.CompletedProcess(argv, 0, "", "")

    pull.pull([f"{SHA}/39-1"], run, tmp_path)
    argv = seen["argv"]
    assert argv[0] == "rsync" and "--files-from=-" in argv and "--safe-links" in argv
    assert not any(a.startswith("--delete") or a in ("--remove-source-files",) for a in argv)
    assert seen["input"] == f"{SHA}/39-1\n" and argv[-1] == f"{tmp_path}/"


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
        argv = [a for a in argv if not a.startswith("--rsync-path")]
        argv = argv[:argv.index("-e")] + argv[argv.index("-e") + 2:]
        argv[-2] = f"{remote}/"
        return subprocess.run(argv, input=kw["input"], capture_output=True, text=True)

    pull.pull([f"{SHA}/39-1"], run, local)
    assert (local / SHA / "39-1" / "frontend" / "manifest.json").read_text() == "dominus"
    assert (local / SHA / "39-1" / "release" / "manifest.json").read_text() == "amarillo"
