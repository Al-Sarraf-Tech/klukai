"""The relay reads amarillo's own CI tree with the same interface as the guest."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ops/amarillo/scripts/klukai-fetch-ci-artifacts.py"
SPEC = importlib.util.spec_from_file_location("klukai_fetch_ci_artifacts_local", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
relay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = relay
SPEC.loader.exec_module(relay)


@pytest.fixture
def local(tmp_path):
    roots = {
        relay.ARTIFACTS: tmp_path / "ci-artifacts",
        relay.OUTBOX: tmp_path / "ci-outbox",
    }
    roots[relay.ARTIFACTS].mkdir()
    return relay.LocalGuest(roots), roots


def test_reads_map_guest_paths_to_the_local_tree(local, tmp_path):
    guest, roots = local
    rev = "a" * 40
    release = roots[relay.ARTIFACTS] / rev / "9-1" / "release"
    release.mkdir(parents=True)
    (release / "manifest.json").write_text("{}")
    dest = tmp_path / "out.json"
    guest.read(f"{relay.ARTIFACTS}/{rev}/9-1/release/manifest.json", dest)
    assert dest.read_text() == "{}"


def test_symlinks_and_missing_files_are_refused(local, tmp_path):
    guest, roots = local
    (roots[relay.ARTIFACTS] / "link").symlink_to("/etc/hostname")
    for path in ("link", "absent"):
        with pytest.raises(RuntimeError):
            guest.read(f"{relay.ARTIFACTS}/{path}", tmp_path / "x")


def test_commands_see_guest_paths_in_and_out(local):
    guest, roots = local
    night = roots[relay.ARTIFACTS] / "nightly" / "2026-10-17T01-30Z-c6d02f0"
    night.mkdir(parents=True)
    (night / "summary.json").write_text("{}")
    out = guest.run(
        ["/usr/bin/find", f"{relay.ARTIFACTS}/nightly", "-name", "summary.json"]
    )
    assert (
        out.strip()
        == f"{relay.ARTIFACTS}/nightly/2026-10-17T01-30Z-c6d02f0/summary.json"
    )


def test_archive_works_on_the_local_tree(local, tmp_path):
    guest, roots = local
    old = roots[relay.ARTIFACTS] / ("b" * 40) / "9-1" / "release"
    old.mkdir(parents=True)
    (old / "manifest.json").write_text("{}")
    (old / "core-image.tar").write_bytes(b"x" * 10)
    dest = tmp_path / "records"
    dest.mkdir()
    result = relay.archive_record(guest, "b" * 40, dest)
    assert Path(result["file"]).name == f"{'b' * 40}.tar"
    assert not (roots[relay.ARTIFACTS] / ("b" * 40)).exists()
    import tarfile

    with tarfile.open(result["file"]) as tar:
        names = tar.getnames()
    assert f"{'b' * 40}/9-1/release/manifest.json" in names
    assert not any(n.endswith("core-image.tar") for n in names)
