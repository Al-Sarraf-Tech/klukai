"""HTTP surface for the Live Portrait and the avatar model."""

from __future__ import annotations

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
    aff = MagicMock()
    aff.get_state = AsyncMock(return_value=SimpleNamespace(level=4))
    router = MagicMock()
    router.is_game_active = AsyncMock(return_value=False)
    monkeypatch.setattr(context, "affection", aff)
    monkeypatch.setattr(context, "router", router)

    async def _user(token):
        return "claude" if token == "good" else None

    app = FastAPI()
    register_portrait_routes(app)
    with patch("app.auth.get_user_from_token", side_effect=_user), \
         patch("app.portrait.current_outfit", new=AsyncMock(return_value="speed_star")), \
         patch("app.portrait.ensure_generation", return_value=True) as ensure, \
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
    def test_pending_then_ready(self, client):
        r = client.get("/api/portrait", headers=AUTH)
        assert r.status_code == 200
        assert r.json() == {"outfit": "speed_star", "status": "pending",
                            "frames": dict.fromkeys(pt.FRAME_NAMES)}
        client.ensure.assert_called_once_with("claude", "speed_star", 4)
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


class TestAvatarModel:
    def test_missing_model_is_json_404(self, client):
        r = client.get("/api/avatar/model", headers=AUTH)
        assert r.status_code == 404 and r.json() == {"error": "Avatar model not found"}

    def test_streams_with_private_cache_and_etag(self, client, tmp_path):
        (tmp_path / "klukai.glb").write_bytes(b"glTF" + b"\x00" * 64)
        r = client.get("/api/avatar/model", headers=AUTH)
        assert r.status_code == 200
        assert r.content.startswith(b"glTF")
        assert r.headers["content-type"] == "model/gltf-binary"
        assert r.headers["cache-control"] == "private, max-age=86400"
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
