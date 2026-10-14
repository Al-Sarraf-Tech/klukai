"""Regression tests for the evidence gate itself, without provider calls."""
import json
from unittest.mock import patch

import pytest

from . import test_system_prompt_snapshots as snapshots


@pytest.fixture
def evidence_dir(tmp_path, monkeypatch):
    directory = tmp_path / "snapshots"
    monkeypatch.setattr(snapshots, "SNAPSHOT_DIR", directory)
    monkeypatch.delenv("UPDATE_SNAPSHOTS", raising=False)
    return directory


def test_missing_snapshot_fails_without_creating_evidence(evidence_dir):
    with pytest.raises(FileNotFoundError, match="UPDATE_SNAPSHOTS=1"):
        snapshots._load_or_create_snapshot("missing.json", {"sha256": "new"})
    assert not evidence_dir.exists()


@pytest.mark.parametrize("setting", ["0", "false", "true", "yes"])
def test_only_exact_update_opt_in_can_write(evidence_dir, monkeypatch, setting):
    evidence_dir.mkdir()
    path = evidence_dir / "existing.json"
    path.write_text('{"sha256": "old"}\n')
    monkeypatch.setenv("UPDATE_SNAPSHOTS", setting)
    result = snapshots._load_or_create_snapshot(path.name, {"sha256": "new"})
    assert result == {"sha256": "old"}
    assert path.read_text() == '{"sha256": "old"}\n'


def test_explicit_update_creates_reviewable_evidence(evidence_dir, monkeypatch):
    monkeypatch.setenv("UPDATE_SNAPSHOTS", "1")
    payload = {"sha256": "new"}
    assert snapshots._load_or_create_snapshot("new.json", payload) == payload
    assert json.loads((evidence_dir / "new.json").read_text()) == payload


@pytest.mark.parametrize("contents", ["not json", "[]", "{}", '{"sha256": 12}'])
def test_invalid_evidence_fails_without_rewriting(evidence_dir, contents):
    evidence_dir.mkdir()
    path = evidence_dir / "bad.json"
    path.write_text(contents)
    with pytest.raises(ValueError):
        snapshots._load_or_create_snapshot(path.name, {"sha256": "expected"})
    assert path.read_text() == contents


def test_assembly_failure_is_a_failure_not_a_skip(evidence_dir):
    with patch("app.personality.system_prompt.assemble_system_prompt", side_effect=ValueError("bad config")):
        with pytest.raises(ValueError, match="bad config"):
            snapshots.test_system_prompt_stable(5, "warm", "morning")


def test_metadata_drift_is_checked_together_with_hash(evidence_dir, monkeypatch):
    with patch("app.personality.system_prompt.assemble_system_prompt", return_value="Klukai addresses Commander."):
        monkeypatch.setenv("UPDATE_SNAPSHOTS", "1")
        snapshots.test_system_prompt_stable(5, "warm", "morning")
        monkeypatch.delenv("UPDATE_SNAPSHOTS")
        path = evidence_dir / "l5_warm_morning.json"
        content = json.loads(path.read_text())
        content["length"] += 1
        path.write_text(json.dumps(content))
        with pytest.raises(AssertionError):
            snapshots.test_system_prompt_stable(5, "warm", "morning")


def test_rotation_does_not_disable_character_invariant(evidence_dir, monkeypatch):
    monkeypatch.setenv("UPDATE_SNAPSHOTS", "1")
    with patch("app.personality.system_prompt.assemble_system_prompt", return_value="Klukai: state your business, Commander."):
        with pytest.raises(AssertionError, match="Cold-flavored"):
            snapshots.test_system_prompt_stable(5, "warm", "morning")
