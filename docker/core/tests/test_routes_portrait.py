"""HTTP surface for the Live Portrait and the avatar model."""

from __future__ import annotations

import asyncio
import gzip
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import context, portrait as pt
from app.routes_portrait import register_portrait_routes

AUTH = {"Authorization": "Bearer good"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    monkeypatch.setenv("AVATAR_MODEL_PATH", str(tmp_path / "klukai.glb"))
    pt._inflight.clear()
    pt._failed_at.clear()
    from app import routes_portrait as rp
    rp._MODEL_GZ.clear()
    rp._MODEL_DIGEST.clear()
    aff = MagicMock()
    aff.get_state = AsyncMock(return_value=SimpleNamespace(level=4))
    router = MagicMock()
    router.is_game_active = AsyncMock(return_value=False)
    monkeypatch.setattr(context, "affection", aff)
    monkeypatch.setattr(context, "router", router)

    async def _user(token):
        return "claude" if token == "good" else None

    def enqueue(user_id, outfit_id, level):
        pt._inflight[pt._key(user_id, outfit_id)] = MagicMock()
        return True

    app = FastAPI()
    register_portrait_routes(app)
    with patch("app.auth.get_user_from_token", side_effect=_user), \
         patch("app.portrait.current_outfit", new=AsyncMock(return_value="speed_star")), \
         patch("app.portrait.ensure_generation", side_effect=enqueue) as ensure, \
         TestClient(app) as c:
        c.router = router  # type: ignore[attr-defined]
        c.ensure = ensure  # type: ignore[attr-defined]
        yield c
    pt._inflight.clear()
    pt._failed_at.clear()


def _frames(frames=pt.FRAME_NAMES, user="claude", outfit="speed_star"):
    d = pt.frame_dir(user, outfit)
    d.mkdir(parents=True, exist_ok=True)
    (d / pt.SOURCE_NAME).write_bytes(b"png")
    for f in frames:
        (d / f"{f}.webp").write_bytes(b"RIFF\x00\x00\x00\x00WEBP" + f.encode())


class TestAuth:
    @pytest.mark.parametrize("method, path", [
        ("get", "/api/portrait"), ("post", "/api/portrait/refresh"), ("get", "/api/avatar/model"),
    ])
    def test_bearer_required(self, client, method, path):
        assert getattr(client, method)(path).status_code == 401
        assert getattr(client, method)(path, headers={"Authorization": "Bearer bad"}).status_code == 401


class TestPortrait:
    def test_cache_only_then_ready(self, client):
        r = client.get("/api/portrait", headers=AUTH)
        assert r.status_code == 200
        assert r.json() == {"outfit": "speed_star", "status": "unavailable",
                            "frames": dict.fromkeys(pt.FRAME_NAMES)}
        client.ensure.assert_not_called()
        _frames()
        body = client.get("/api/portrait", headers=AUTH).json()
        assert body["status"] == "ready"
        # Every URL works as a plain <img src>: no Authorization header at all.
        for f in pt.FRAME_NAMES:
            img = client.get(body["frames"][f])
            assert img.status_code == 200
            assert img.headers["content-type"] == "image/webp"
            assert img.headers["cache-control"] == "private, max-age=1800"
            assert img.content.endswith(f.encode())

    def test_unavailable_while_gaming(self, client):
        client.router.is_game_active = AsyncMock(return_value=True)
        assert client.get("/api/portrait", headers=AUTH).json()["status"] == "unavailable"
        client.ensure.assert_not_called()


class TestAccountWorkerMode:
    """Off the GPU (images on the account worker) Dominus is never asked."""

    @pytest.fixture
    def account(self, client, monkeypatch):
        monkeypatch.setenv("KLUKAI_IMAGE_PROVIDER", "agy")
        client.router.is_game_active = AsyncMock(side_effect=AssertionError("Dominus probed"))
        return client

    def test_get_never_probes_the_game_guard(self, account):
        _frames(frames=("base",))
        body = account.get("/api/portrait", headers=AUTH).json()
        assert body["status"] == "ready"
        assert body["frames"]["base"] and body["frames"]["blink"] is None

    def test_refresh_keeps_the_cached_set(self, account):
        _frames()
        r = account.post("/api/portrait/refresh", headers=AUTH)
        assert r.status_code == 409 and r.json()["code"] == "REDRAW_UNAVAILABLE"
        assert pt.existing_frames("claude", "speed_star") == set(pt.FRAME_NAMES)
        account.ensure.assert_not_called()


class TestFrames:
    def test_bad_or_foreign_signature_is_forbidden(self, client):
        _frames()
        url = client.get("/api/portrait", headers=AUTH).json()["frames"]["blink"]
        assert client.get(url.split("&sig=")[0]).status_code == 403
        assert client.get(url.replace("/blink.webp", "/talk.webp")).status_code == 403
        assert client.get(url.replace("/claude/", "/jalsarraf/")).status_code == 403

    def test_unknown_frame_or_unsafe_id_is_not_found(self, client):
        assert client.get("/api/portrait/frame/claude/speed_star/wink.webp?sig=x").status_code == 404
        assert client.get("/api/portrait/frame/claude/bad..id/base.webp?sig=x").status_code == 404

    def test_non_ascii_signature_is_forbidden_not_an_error(self, client):
        """compare_digest on str raises TypeError for non-ASCII input; a
        `?sig=1.ü` must be refused like any bad signature, never a 500."""
        _frames()
        for sig in ("9999999999.%C3%BC", "9999999999.1.%C3%BC", "%C3%BC.x"):
            r = client.get(f"/api/portrait/frame/claude/speed_star/base.webp?sig={sig}")
            assert r.status_code == 403, sig

    def test_signed_but_missing_file_is_not_found(self, client):
        url = pt.frame_url("claude", "speed_star", "smile", 1)
        assert client.get(url).status_code == 404


class TestRefresh:
    def test_refresh_wipes_and_restarts(self, client):
        _frames()
        r = client.post("/api/portrait/refresh", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["status"] == "pending"
        assert pt.existing_frames("claude", "speed_star") == set()

    def test_refused_during_a_game(self, client):
        _frames()
        client.router.is_game_active = AsyncMock(return_value=True)
        r = client.post("/api/portrait/refresh", headers=AUTH)
        assert r.status_code == 409 and r.json()["code"] == "GPU_BUSY"
        assert pt.existing_frames("claude", "speed_star") == set(pt.FRAME_NAMES)

    def test_refused_while_already_drawing(self, client):
        pt._inflight[pt._key("claude", "speed_star")] = MagicMock()
        r = client.post("/api/portrait/refresh", headers=AUTH)
        assert r.status_code == 409 and r.json()["code"] == "IN_PROGRESS"

    def test_rate_limited_to_once_per_ten_minutes(self):
        from app import main, rate_limit
        assert main._bucket_for_path("/api/portrait/refresh") == "portrait_refresh"
        limit = rate_limit.LIMITS["portrait_refresh"]
        assert (limit.requests, limit.window_seconds) == (1, 600)
        assert main._bucket_for_path("/api/portrait") is None

    def test_a_409_gives_the_ten_minute_token_back(self, client):
        """Through the real app (middleware + handler): the limiter charges
        before the handler runs, so a GPU-busy 409 must refund — otherwise
        nothing rendered and he is still 429'd once the game ends."""
        from fastapi.testclient import TestClient
        from app import main

        client.router.is_game_active = AsyncMock(return_value=True)
        from app.rate_limit import Consumption

        receipt = Consumption("k", 1)
        with patch(
            "app.rate_limit.consume", new=AsyncMock(return_value=(0, 600, receipt))
        ) as cac, patch(
            "app.rate_limit.refund", new=AsyncMock(return_value=True)
        ) as refund:
            r = TestClient(main.app).post("/api/portrait/refresh", headers=AUTH)
        assert r.status_code == 409 and r.json()["code"] == "GPU_BUSY"
        cac.assert_awaited_once_with("claude", "portrait_refresh")
        refund.assert_awaited_once_with(receipt)
        assert r.headers["x-ratelimit-remaining"] == "1"


class TestAvatarModel:
    def test_catalog_is_private_and_reports_actual_availability(self, client, tmp_path):
        assert client.get("/api/avatar/models").status_code == 401
        (tmp_path / "klukai.glb").write_bytes(b"default")
        r = client.get("/api/avatar/models", headers=AUTH)
        assert r.status_code == 200
        assert r.headers["cache-control"] == "private, no-cache"
        data = r.json()
        assert data["notice"] == "For personal, noncommercial use only."
        entries = {m["id"]: m for m in data["models"]}
        assert entries["default"]["available"] is True
        assert "hk416_gfl64" not in entries
        assert entries["hk416_vroid"]["available"] is False
        assert entries["klukai_garf"]["available"] is False
        for model in ("garf_dorm", "garf_speed_star", "garf_astral_luminous", "garf_cerulean_breaker"):
            assert entries[model]["available"] is False
        assert str(tmp_path) not in r.text

    @pytest.mark.parametrize("model", ["../klukai", "/etc/passwd", "unknown", ""])
    def test_unlisted_model_never_falls_back(self, client, tmp_path, model):
        (tmp_path / "klukai.glb").write_bytes(b"default")
        assert client.get("/api/avatar/model", params={"model": model}, headers=AUTH).status_code == 404

    def test_retired_retro_model_is_inaccessible_even_if_file_remains(self, client, tmp_path):
        variant = tmp_path / "variants" / "hk416_gfl64.glb"
        variant.parent.mkdir()
        variant.write_bytes(b"retired model")
        assert client.get("/api/avatar/model?model=hk416_gfl64", headers=AUTH).status_code == 404

    def test_models_have_distinct_etags_even_with_same_size_and_time(self, client, tmp_path):
        import os
        default = tmp_path / "klukai.glb"
        default.write_bytes(b"one")
        variant = tmp_path / "variants" / "garf_dorm.glb"
        variant.parent.mkdir()
        variant.write_bytes(b"two")
        os.utime(variant, ns=(default.stat().st_atime_ns, default.stat().st_mtime_ns))
        first = client.get("/api/avatar/model", headers=AUTH)
        second = client.get("/api/avatar/model?model=garf_dorm", headers={**AUTH, "If-None-Match": first.headers["etag"]})
        assert second.status_code == 200 and second.content == b"two"
        assert second.headers["etag"] != first.headers["etag"]

    def test_missing_variant_does_not_serve_default(self, client, tmp_path):
        (tmp_path / "klukai.glb").write_bytes(b"default")
        assert client.get("/api/avatar/model?model=hk416_vroid", headers=AUTH).status_code == 404

    def test_missing_model_is_json_404(self, client):
        r = client.get("/api/avatar/model", headers=AUTH)
        assert r.status_code == 404 and r.json() == {"error": "Avatar model not found"}

    def test_streams_with_private_cache_and_etag(self, client, tmp_path):
        (tmp_path / "klukai.glb").write_bytes(b"glTF" + b"\x00" * 64)
        r = client.get("/api/avatar/model", headers=AUTH)
        assert r.status_code == 200
        assert r.content.startswith(b"glTF")
        assert r.headers["content-type"] == "model/gltf-binary"
        assert r.headers["cache-control"] == "private, no-cache"  # a rebuild reaches him at once
        assert r.headers["vary"] == "Accept-Encoding, Authorization"
        etag = r.headers["etag"]
        assert etag.startswith('"') and etag.endswith('"')

        again = client.get("/api/avatar/model", headers={**AUTH, "If-None-Match": etag})
        assert again.status_code == 304 and again.content == b""
        assert again.headers["etag"] == etag
        weak = client.get("/api/avatar/model", headers={**AUTH, "If-None-Match": f'"x", W/{etag}'})
        assert weak.status_code == 304
        star = client.get("/api/avatar/model", headers={**AUTH, "If-None-Match": "*"})
        assert star.status_code == 304
        stale = client.get("/api/avatar/model", headers={**AUTH, "If-None-Match": '"old"'})
        assert stale.status_code == 200

    def test_gzip_when_accepted_and_compressed_once_per_version(self, client, tmp_path):
        from app import routes_portrait

        routes_portrait._MODEL_GZ.clear()
        (tmp_path / "klukai.glb").write_bytes(b"glTF" + b"\x01" * 4096)
        plain = client.get("/api/avatar/model", headers={**AUTH, "Accept-Encoding": "identity"})
        assert "content-encoding" not in plain.headers
        assert plain.content.startswith(b"glTF")
        gz = client.get("/api/avatar/model", headers={**AUTH, "Accept-Encoding": "gzip, br"})
        assert gz.headers["content-encoding"] == "gzip"
        assert gz.content.startswith(b"glTF")  # the client inflates it
        assert int(gz.headers["content-length"]) < 4100
        assert list(routes_portrait._MODEL_GZ) == [gz.headers["etag"]]
        cached = routes_portrait._MODEL_GZ[gz.headers["etag"]]
        client.get("/api/avatar/model", headers={**AUTH, "Accept-Encoding": "gzip"})
        assert routes_portrait._MODEL_GZ[gz.headers["etag"]] is cached

    def test_gzip_runs_off_the_event_loop_and_is_byte_exact(self, client, tmp_path):
        """The first gzip of a tens-of-MB model must not stall the loop (and
        the chat WebSocket on it): read + deflate run in a worker thread,
        once per version, and the cached bytes are exactly the file deflated."""
        from app import routes_portrait

        routes_portrait._MODEL_GZ.clear()
        raw = b"glTF" + bytes(range(256)) * 64
        model = tmp_path / "klukai.glb"
        model.write_bytes(raw)
        real = routes_portrait._gzip_file
        calls: list[tuple[Path, bool]] = []

        def spy(path):
            try:
                asyncio.get_running_loop()
                on_loop = True
            except RuntimeError:
                on_loop = False
            calls.append((path, on_loop))
            return real(path)

        with patch.object(routes_portrait, "_gzip_file", spy):
            first = client.get("/api/avatar/model", headers={**AUTH, "Accept-Encoding": "gzip"})
            second = client.get("/api/avatar/model", headers={**AUTH, "Accept-Encoding": "gzip"})
        assert calls == [(model, False)]  # once, and not on a thread running a loop
        assert first.headers["content-encoding"] == "gzip"
        assert first.headers["etag"] == second.headers["etag"]
        assert first.content == raw == second.content  # the client inflates it
        assert gzip.decompress(routes_portrait._MODEL_GZ[first.headers["etag"]]) == raw

    def test_etag_is_a_strong_content_hash_that_survives_a_touch(self, client, tmp_path):
        import hashlib
        import os
        raw = b"glTF" + bytes(range(256)) * 8
        model = tmp_path / "klukai.glb"
        model.write_bytes(raw)
        plain = {**AUTH, "Accept-Encoding": "identity"}
        first = client.get("/api/avatar/model", headers=plain).headers["etag"]
        assert first == f'"default-{hashlib.sha256(raw).hexdigest()[:32]}"'
        assert not first.startswith("W/")
        # Same bytes, new mtime (a redeploy copying the file): same tag, so
        # his cached copy stays good.
        st = model.stat()
        os.utime(model, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        assert client.get("/api/avatar/model", headers=plain).headers["etag"] == first
        model.write_bytes(raw + b"rebuilt")
        changed = client.get("/api/avatar/model", headers={**plain, "If-None-Match": first})
        assert changed.status_code == 200 and changed.content.endswith(b"rebuilt")
        assert changed.headers["etag"] != first

    def test_gzip_and_identity_are_distinct_strong_representations(self, client, tmp_path):
        (tmp_path / "klukai.glb").write_bytes(b"glTF" + b"\x02" * 2048)
        plain = client.get("/api/avatar/model", headers={**AUTH, "Accept-Encoding": "identity"})
        gz = client.get("/api/avatar/model", headers={**AUTH, "Accept-Encoding": "gzip"})
        assert gz.headers["etag"] == plain.headers["etag"][:-1] + '-gz"'
        # A cached identity copy can't be revalidated into a gzip response …
        cross = client.get("/api/avatar/model", headers={**AUTH, "Accept-Encoding": "gzip", "If-None-Match": plain.headers["etag"]})
        assert cross.status_code == 200 and cross.headers["content-encoding"] == "gzip"
        # … each variant revalidates against its own tag.
        same = client.get("/api/avatar/model", headers={**AUTH, "Accept-Encoding": "gzip", "If-None-Match": gz.headers["etag"]})
        assert same.status_code == 304 and same.headers["etag"] == gz.headers["etag"]
        ident = client.get("/api/avatar/model", headers={**AUTH, "Accept-Encoding": "identity", "If-None-Match": plain.headers["etag"]})
        assert ident.status_code == 304

    def test_hash_once_per_file_version_and_a_304_never_reads_the_file(self, client, tmp_path):
        """The content hash is computed once per (size, mtime), off the event
        loop. A revalidation after that is a stat and a 304: the model is not
        opened, hashed or compressed again."""
        from app import routes_portrait

        model = tmp_path / "klukai.glb"
        model.write_bytes(b"glTF" + b"\x03" * 4096)
        real = routes_portrait._digest_file
        calls: list[tuple[Path, bool]] = []

        def spy(path):
            try:
                asyncio.get_running_loop()
                on_loop = True
            except RuntimeError:
                on_loop = False
            calls.append((path, on_loop))
            return real(path)

        with patch.object(routes_portrait, "_digest_file", spy):
            first = client.get("/api/avatar/model", headers=AUTH)
            again = client.get("/api/avatar/model", headers=AUTH)
        assert first.status_code == again.status_code == 200
        assert calls == [(model, False)]
        etag = first.headers["etag"]

        def no_reads(*_a, **_k):
            raise AssertionError("a 304 must not read the model")

        with patch.object(routes_portrait, "_digest_file", no_reads), \
             patch.object(routes_portrait, "_gzip_file", no_reads), \
             patch.object(Path, "read_bytes", no_reads), \
             patch.object(Path, "open", no_reads):
            r = client.get("/api/avatar/model", headers={**AUTH, "If-None-Match": etag})
        assert r.status_code == 304 and r.content == b""
        assert r.headers["etag"] == etag
        assert r.headers["cache-control"] == "private, no-cache"
        assert r.headers["vary"] == "Accept-Encoding, Authorization"

    def test_a_model_rewritten_while_hashed_is_hashed_again(self, client, tmp_path):
        """If the file changes during hashing, that hash is not kept for the
        new version: the next request hashes the new bytes."""
        from app import routes_portrait

        model = tmp_path / "klukai.glb"
        model.write_bytes(b"glTF-old")
        real = routes_portrait._digest_file
        calls: list[Path] = []

        def racing(path):
            digest = real(path)
            if not calls:
                path.write_bytes(b"glTF-new, longer")  # a deploy lands mid-hash
            calls.append(path)
            return digest

        plain = {**AUTH, "Accept-Encoding": "identity"}
        with patch.object(routes_portrait, "_digest_file", racing):
            client.get("/api/avatar/model", headers=plain)
            assert routes_portrait._MODEL_DIGEST == {}
            r = client.get("/api/avatar/model", headers=plain)
        assert len(calls) == 2
        assert r.content == b"glTF-new, longer"
        import hashlib
        assert r.headers["etag"] == f'"default-{hashlib.sha256(b"glTF-new, longer").hexdigest()[:32]}"'

    @pytest.mark.parametrize("auth", [{}, {"Authorization": "Bearer bad"}, {"Authorization": "good"}])
    def test_auth_comes_before_any_304(self, client, tmp_path, auth):
        """Without a valid bearer: 401, never a 304 (which would confirm his
        cached copy) and never the bytes, whatever validator is sent."""
        (tmp_path / "klukai.glb").write_bytes(b"glTF" + b"\x04" * 64)
        etag = client.get("/api/avatar/model", headers=AUTH).headers["etag"]
        for validator in (etag, "*"):
            r = client.get("/api/avatar/model", headers={**auth, "If-None-Match": validator})
            assert r.status_code == 401
            assert "etag" not in r.headers
            assert b"glTF" not in r.content

    def test_never_cacheable_by_a_shared_cache(self, client, tmp_path):
        """Cloudflare sits in front of the site: the model must stay private."""
        (tmp_path / "klukai.glb").write_bytes(b"glTF" + b"\x05" * 64)
        for enc in ("identity", "gzip"):
            r = client.get("/api/avatar/model", headers={**AUTH, "Accept-Encoding": enc})
            cc = [d.strip() for d in r.headers["cache-control"].split(",")]
            assert "private" in cc and "no-cache" in cc
            assert not {"public", "immutable"} & set(cc)
            assert not any(d.startswith("s-maxage") for d in cc)
            assert {"Accept-Encoding", "Authorization"} <= {v.strip() for v in r.headers["vary"].split(",")}

    def test_a_model_removed_mid_request_is_a_404_not_an_error(self, client, tmp_path):
        from app import routes_portrait

        (tmp_path / "klukai.glb").write_bytes(b"glTF")

        def gone(_path):
            raise FileNotFoundError("removed")

        with patch.object(routes_portrait, "_digest_file", gone):
            r = client.get("/api/avatar/model", headers=AUTH)
        assert r.status_code == 404 and r.json() == {"error": "Avatar model not found"}

    def test_default_path(self, client, monkeypatch):
        monkeypatch.delenv("AVATAR_MODEL_PATH")
        from app import routes_portrait
        assert routes_portrait.DEFAULT_AVATAR_MODEL_PATH == "/avatar/klukai.glb"
        assert client.get("/api/avatar/model", headers=AUTH).status_code == 404


def test_registered_on_the_real_app():
    from app.main import app
    paths = {getattr(r, "path", "") for r in app.routes}
    for p in ("/api/portrait", "/api/portrait/refresh", "/api/avatar/model",
              "/api/portrait/frame/{user_id}/{outfit_id}/{frame}.webp"):
        assert p in paths


def test_real_app_middleware_keeps_the_model_cache_headers(tmp_path, monkeypatch):
    """Through the real app's middleware stack (CORS, security headers,
    request id, rate limit, metrics): the private validator headers arrive
    unchanged, a 304 stays a 304, and no auth still means 401."""
    from app import routes_portrait
    from app.main import app

    model = tmp_path / "klukai.glb"
    model.write_bytes(b"glTF" + b"\x06" * 128)
    monkeypatch.setenv("AVATAR_MODEL_PATH", str(model))
    routes_portrait._MODEL_GZ.clear()
    routes_portrait._MODEL_DIGEST.clear()

    async def _user(token):
        return "claude" if token == "good" else None

    with patch("app.auth.get_user_from_token", side_effect=_user):
        c = TestClient(app)  # no lifespan: only the HTTP path is exercised
        r = c.get("/api/avatar/model", headers=AUTH)
        assert r.status_code == 200 and r.content.startswith(b"glTF")
        assert r.headers["cache-control"] == "private, no-cache"
        assert r.headers["vary"] == "Accept-Encoding, Authorization"
        assert r.headers["x-content-type-options"] == "nosniff"
        again = c.get("/api/avatar/model", headers={**AUTH, "If-None-Match": r.headers["etag"]})
        assert again.status_code == 304 and again.content == b""
        assert again.headers["etag"] == r.headers["etag"]
        assert c.get("/api/avatar/model", headers={"If-None-Match": r.headers["etag"]}).status_code == 401
    routes_portrait._MODEL_GZ.clear()
    routes_portrait._MODEL_DIGEST.clear()
