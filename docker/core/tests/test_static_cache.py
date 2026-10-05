"""Cache policy for the PWA static files (iOS/Brave: local CanvasKit + fonts)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.static_cache import IMMUTABLE, ONE_DAY, REVALIDATE, CachedStaticFiles, cache_control_for


@pytest.mark.parametrize("path,policy", [
    ("main.dart.75939d70.js", IMMUTABLE),
    ("/flutter_bootstrap.c898620b.js", IMMUTABLE),
    ("canvaskit/canvaskit.wasm", ONE_DAY),
    ("canvaskit/chromium/canvaskit.js", ONE_DAY),
    ("fonts/NotoSansJP-Regular.ttf", ONE_DAY),
    ("assets/fonts/MaterialIcons-Regular.otf", ONE_DAY),
    ("icons/Icon-192.png", ONE_DAY),
    ("assets/assets/klukai_portrait.png", ONE_DAY),
    ("index.html", REVALIDATE),
    ("companion.html", REVALIDATE),
    ("companion/app.js", REVALIDATE),
    ("main.dart.js", REVALIDATE),  # unhashed: never pinned
    ("flutter_bootstrap.js", REVALIDATE),
    ("manifest.json", REVALIDATE),
    ("", REVALIDATE),
])
def test_policy(path, policy):
    assert cache_control_for(path) == policy


def test_mounted_files_carry_the_header(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "main.dart.0123abcd.js").write_text("x")
    (tmp_path / "canvaskit").mkdir()
    (tmp_path / "canvaskit" / "canvaskit.wasm").write_bytes(b"\0asm")
    app = FastAPI()
    app.mount("/app", CachedStaticFiles(directory=str(tmp_path), html=True), name="pwa")
    c = TestClient(app)
    assert c.get("/app/").headers["cache-control"] == REVALIDATE
    assert c.get("/app/main.dart.0123abcd.js").headers["cache-control"] == IMMUTABLE
    r = c.get("/app/canvaskit/canvaskit.wasm")
    assert r.headers["cache-control"] == ONE_DAY
    etag = r.headers["etag"]
    again = c.get("/app/canvaskit/canvaskit.wasm", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.headers["cache-control"] == ONE_DAY
    missing = c.get("/app/nope.js")
    assert missing.status_code == 404
    assert "cache-control" not in missing.headers
