"""Unit tests for the xAI key scan. The live tree is checked in CI."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.ci.xai_secret_scan import scan_text, tracked_hits  # noqa: E402


def test_a_key_shaped_token_is_rejected_and_a_short_one_is_not():
    assert scan_text("prefix " + "xai-" + ("a" * 20) + " suffix")
    assert not scan_text("xai-" + ("a" * 19))
    assert not scan_text("unit-test-key")


def test_tracked_hits_reads_only_the_git_index(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    tracked = repo / "note.txt"
    tracked.write_text("token " + "xai-" + ("b" * 24) + "\n")
    ignored = repo / "scratch.txt"
    ignored.write_text("xai-" + ("c" * 24) + "\n")
    subprocess.run(["git", "add", "note.txt"], cwd=repo, check=True, capture_output=True)
    assert tracked_hits(repo) == ["note.txt"]
