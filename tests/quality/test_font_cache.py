"""Cached fonts must be verified, offline-capable and fetched concurrently."""

from hashlib import sha256
from pathlib import Path
import importlib.util
import json
import threading

import pytest


def module():
    assert importlib.util.find_spec("tools.ci.font_cache") is not None, (
        "verified font cache is missing"
    )
    from tools.ci import font_cache

    return font_cache


def fixture(tmp_path, entries=None):
    entries = entries or {"noto/v1/font.woff2": b"font bytes"}
    source = tmp_path / "main.dart.js"
    source.write_text(",".join(json.dumps(p) for p in entries))
    lock = tmp_path / "fonts.json"
    lock.write_text(
        json.dumps(
            {
                "schema": 1,
                "files": {
                    p: {"sha256": sha256(b).hexdigest(), "size": len(b)}
                    for p, b in entries.items()
                },
            }
        )
    )
    return source, tmp_path / "cache", tmp_path / "output", lock


def test_verified_cache_builds_offline(tmp_path, monkeypatch):
    m = module()
    source, cache, output, lock = fixture(tmp_path)
    font = cache / "noto/v1/font.woff2"
    font.parent.mkdir(parents=True)
    font.write_bytes(b"font bytes")
    monkeypatch.setattr(
        m, "download", lambda *a: pytest.fail("valid cache must be offline")
    )
    assert m.mirror(source, cache, output, lock)["downloaded"] == 0
    assert (output / "noto/v1/font.woff2").read_bytes() == b"font bytes"


def test_corrupted_cache_is_repaired_before_publication(tmp_path, monkeypatch):
    m = module()
    source, cache, output, lock = fixture(tmp_path)
    font = cache / "noto/v1/font.woff2"
    font.parent.mkdir(parents=True)
    font.write_bytes(b"corrupt")
    monkeypatch.setattr(m, "download", lambda *a: b"font bytes")
    assert m.mirror(source, cache, output, lock)["downloaded"] == 1
    assert (
        font.read_bytes()
        == (output / "noto/v1/font.woff2").read_bytes()
        == b"font bytes"
    )


def test_wrong_download_is_not_cached_or_published(tmp_path, monkeypatch):
    m = module()
    source, cache, output, lock = fixture(tmp_path)
    monkeypatch.setattr(m, "download", lambda *a: b"wrong bytes")
    with pytest.raises(ValueError, match="digest|size"):
        m.mirror(source, cache, output, lock)
    assert not (cache / "noto/v1/font.woff2").exists()
    assert not output.exists()


def test_unknown_font_fails_before_network(tmp_path, monkeypatch):
    m = module()
    source, cache, output, lock = fixture(tmp_path)
    source.write_text('"unreviewed/v2/font.woff2"')
    monkeypatch.setattr(m, "download", lambda *a: pytest.fail("unlocked download"))
    with pytest.raises(ValueError, match="unpinned"):
        m.mirror(source, cache, output, lock)


def test_misses_overlap_with_bounded_workers(tmp_path, monkeypatch):
    m = module()
    source, cache, output, lock = fixture(
        tmp_path, {f"noto/v1/font{i}.woff2": b"font bytes" for i in range(4)}
    )
    barrier = threading.Barrier(2, timeout=3)
    active = 0
    peak = 0
    guard = threading.Lock()

    def fetch(*args):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        barrier.wait()
        with guard:
            active -= 1
        return b"font bytes"

    monkeypatch.setattr(m, "download", fetch)
    assert m.mirror(source, cache, output, lock, workers=2)["downloaded"] == 4
    assert peak == 2
    assert len(list(output.rglob("*.woff2"))) == 4


def test_shared_cache_symlink_is_rejected(tmp_path, monkeypatch):
    m = module()
    source, cache, output, lock = fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    cache.mkdir()
    (cache / "noto").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(m, "download", lambda *a: b"font bytes")
    with pytest.raises(ValueError, match="symlink"):
        m.mirror(source, cache, output, lock)
    assert not list(outside.iterdir())


@pytest.mark.parametrize("pinned", [True, False])
def test_build_only_mirrors_verified_fonts_and_preserves_served_bundle(
    tmp_path, pinned
):
    import os
    import shutil
    import subprocess

    root = Path(__file__).resolve().parents[2]
    repo = tmp_path / "repo"
    for name in ("scripts/deploy-web.sh", "tools/ci/font_cache.py"):
        destination = repo / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, destination)
    source, cache, output, lock = fixture(tmp_path)
    if not pinned:
        lock.write_text('{"schema":1,"files":{}}')
    shutil.copyfile(lock, repo / "tools/ci/fallback-fonts.lock.json")
    font = cache / "noto/v1/font.woff2"
    font.parent.mkdir(parents=True)
    font.write_bytes(b"font bytes")
    build = repo / "flutter_app/build/web"
    build.mkdir(parents=True)
    shutil.copyfile(source, build / "main.dart.js")
    (build / "flutter_bootstrap.js").write_text(
        'const klukaiFontFallbackBase = null;\n{"useLocalCanvasKit":true}'
    )
    web = repo / "flutter_app/web"
    web.mkdir()
    (web / "login.html").write_text("login")
    served = repo / "web-build"
    served.mkdir()
    (served / "index.html").write_text("existing served page")
    flutter = tmp_path / "flutter"
    flutter.write_text("#!/bin/sh\nexit 0\n")
    flutter.chmod(0o755)
    run = subprocess.run(
        ["bash", str(repo / "scripts/deploy-web.sh"), "--build-only"],
        env={
            **os.environ,
            "FLUTTER": str(flutter),
            "FONT_CACHE": str(cache),
            "KLUKAI_ALLOW_FONT_CDN": "0",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (run.returncode == 0) is pinned, run.stdout + run.stderr
    assert (served / "index.html").read_text() == "existing served page"
    if pinned:
        assert (
            build / "fonts/fallback/noto/v1/font.woff2"
        ).read_bytes() == b"font bytes"
        assert (
            'klukaiFontFallbackBase = "fonts/fallback/"'
            in (build / "flutter_bootstrap.js").read_text()
        )
    else:
        assert not (build / "fonts/fallback").exists()
