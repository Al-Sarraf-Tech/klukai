"""Behavioral coverage tests for core handlers in app.routes.

Targets the uncovered lines in routes.py: the Bearer-token extractor
(_get_user_id), the health probes (cached/live/ready incl. 503), the deep
per-subsystem health aggregation, the TTS proxy, the generate-image success
path, the gift tier branches (favoured/liked + ws broadcast + audit), and the
_run_mission background narrative helper.

Pattern follows test_routes_auth_affection.py / test_routes_gameplay.py:
build the app via register_routes, locate the closure handler by path+method,
and call it directly with a mocked Request. Per the task brief these handlers
live in routes.py so app.routes.* is the correct patch target. All network /
DB / LLM calls are mocked; asyncio.sleep is patched so _run_mission's 15s
delay is instant and deterministic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Helpers ─────────────────────────────────────────────────────────────────


def _mk_request(token: str | None = "good") -> MagicMock:
    req = MagicMock()
    req.headers = {"Authorization": f"Bearer {token}"} if token else {}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    req.state = MagicMock()
    req.state.request_id = "test-req-id"
    return req


def _app_with_routes() -> FastAPI:
    from app.routes import register_routes
    app = FastAPI()
    register_routes(app)
    return app


def _find_route(app: FastAPI, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _mk_aff_state(score: int = 500, level: int = 5):
    return SimpleNamespace(
        score=score, level=level, level_name="Trusted",
        consecutive_days=7, total_interactions=100, first_interaction=None,
    )


class _Resp:
    """Minimal httpx.Response stand-in for subsystem/TTS probes."""

    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.content = content

    def json(self):
        return self._json


def _fake_async_client(get_resp=None, post_resp=None, get_exc=None, post_exc=None):
    """Build a fake `async with httpx.AsyncClient(...) as c` context manager."""
    client = MagicMock()
    client.get = AsyncMock(side_effect=get_exc) if get_exc else AsyncMock(return_value=get_resp)
    client.post = AsyncMock(side_effect=post_exc) if post_exc else AsyncMock(return_value=post_resp)
    client.ping = AsyncMock()
    client.aclose = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    ctx._client = client  # expose for assertions
    return ctx


# ═══════════════════════════════════════════════════════════════════════════
# _get_user_id — Bearer token extraction (lines 82-87)
# ═══════════════════════════════════════════════════════════════════════════


class TestGetUserId:
    @pytest.mark.asyncio
    async def test_extracts_user_from_bearer_token(self):
        from app.routes import _get_user_id
        with patch("app.auth.get_user_from_token",
                   new=AsyncMock(return_value="alice")) as gut:
            result = await _get_user_id(_mk_request(token="abc123"))
        assert result == "alice"
        gut.assert_awaited_once_with("abc123")  # the 7-char "Bearer " prefix stripped

    @pytest.mark.asyncio
    async def test_returns_none_without_bearer_prefix(self):
        from app.routes import _get_user_id
        req = MagicMock()
        req.headers = {"Authorization": "Basic something"}
        with patch("app.auth.get_user_from_token", new=AsyncMock()) as gut:
            result = await _get_user_id(req)
        assert result is None
        gut.assert_not_called()  # non-Bearer never reaches token validation

    @pytest.mark.asyncio
    async def test_returns_none_when_header_absent(self):
        from app.routes import _get_user_id
        req = MagicMock()
        req.headers = {}
        result = await _get_user_id(req)
        assert result is None

    @pytest.mark.asyncio
    async def test_propagates_invalid_token_none(self):
        from app.routes import _get_user_id
        with patch("app.auth.get_user_from_token", new=AsyncMock(return_value=None)):
            result = await _get_user_id(_mk_request(token="expired"))
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# Health probes (lines 122-123, 133-134, 144-151)
# ═══════════════════════════════════════════════════════════════════════════


class TestHealthProbes:
    @pytest.mark.asyncio
    async def test_cached_health_returns_payload(self):
        app = _app_with_routes()
        handler = _find_route(app, "/health", "GET")
        payload = {"status": "ok", "cached": True}
        with patch("app.observability.health_cache.get_cached_health",
                   new=AsyncMock(return_value=payload)):
            result = await handler()
        assert result == payload

    @pytest.mark.asyncio
    async def test_live_health_is_process_only(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/health/live", "GET")
        with patch("app.observability.health_cache.get_live_health",
                   return_value={"status": "alive"}):
            result = await handler()
        assert result == {"status": "alive"}

    @pytest.mark.asyncio
    async def test_ready_returns_payload_when_healthy(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/health/ready", "GET")
        fresh = {"status": "ok", "backends": "up"}
        with patch("app.observability.health_cache.get_fresh_health",
                   new=AsyncMock(return_value=fresh)):
            result = await handler()
        assert result == fresh

    @pytest.mark.asyncio
    async def test_ready_returns_503_when_unhealthy(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/health/ready", "GET")
        with patch("app.observability.health_cache.get_fresh_health",
                   new=AsyncMock(return_value={"status": "unhealthy"})):
            resp = await handler()
        # Readiness failure must surface as 503 so LBs drain the pod.
        assert resp.status_code == 503


# ═══════════════════════════════════════════════════════════════════════════
# Subsystem health aggregation (lines 163-250)
# ═══════════════════════════════════════════════════════════════════════════


class TestSubsystemHealth:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/health/subsystems", "GET")
        with patch("app.routes._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(_mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_all_ok_overall_ok(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/health/subsystems", "GET")

        # httpx client returns 200 + healthy JSON for every probe.
        good = _Resp(200, {
            "data": [{"id": "model-a"}],
            "devices": [{"name": "RTX 3090", "vram_free": 24_000_000_000}],
        })
        redis_mock = MagicMock()
        redis_mock.ping = AsyncMock()
        redis_mock.aclose = AsyncMock()

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.db.check_health", new=AsyncMock(return_value={"status": "ok"})), \
             patch("redis.asyncio.from_url", return_value=redis_mock), \
             patch("httpx.AsyncClient", return_value=_fake_async_client(get_resp=good)):
            result = await handler(_mk_request())

        assert result["status"] == "ok"
        subs = result["subsystems"]
        assert subs["database"]["status"] == "ok"
        assert subs["redis"]["status"] == "ok"
        assert subs["qdrant"]["status"] == "ok"
        assert subs["lm_studio"]["status"] == "ok"
        assert subs["lm_studio"]["models_loaded"] == 1
        assert subs["lm_studio"]["models"] == ["model-a"]
        assert subs["comfyui"]["status"] == "ok"
        assert subs["comfyui"]["gpu"] == "RTX 3090"
        assert subs["comfyui"]["vram_free_gb"] == 24.0
        assert subs["embeddings"]["status"] == "ok"
        assert subs["voice"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_database_down_is_critical(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/health/subsystems", "GET")

        good = _Resp(200, {"data": [], "devices": [{}]})
        redis_mock = MagicMock()
        redis_mock.ping = AsyncMock()
        redis_mock.aclose = AsyncMock()

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.db.check_health", new=AsyncMock(side_effect=RuntimeError("pg gone"))), \
             patch("redis.asyncio.from_url", return_value=redis_mock), \
             patch("httpx.AsyncClient", return_value=_fake_async_client(get_resp=good)):
            result = await handler(_mk_request())

        # DB down dominates -> overall "critical".
        assert result["subsystems"]["database"]["status"] == "down"
        assert result["status"] == "critical"

    @pytest.mark.asyncio
    async def test_non_db_outage_is_degraded(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/health/subsystems", "GET")

        # DB ok, but every HTTP probe + redis fails -> degraded (not critical).
        redis_mock = MagicMock()
        redis_mock.ping = AsyncMock(side_effect=RuntimeError("redis gone"))
        redis_mock.aclose = AsyncMock()

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.db.check_health", new=AsyncMock(return_value={"status": "ok"})), \
             patch("redis.asyncio.from_url", return_value=redis_mock), \
             patch("httpx.AsyncClient",
                   return_value=_fake_async_client(get_exc=RuntimeError("net down"))):
            result = await handler(_mk_request())

        assert result["subsystems"]["database"]["status"] == "ok"
        assert result["subsystems"]["qdrant"]["status"] == "down"
        assert result["subsystems"]["lm_studio"]["status"] == "down"
        assert result["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_degraded_when_probe_returns_non_200(self):
        """Qdrant/embeddings/voice report 'degraded' on a non-200 (not down)."""
        app = _app_with_routes()
        handler = _find_route(app, "/api/health/subsystems", "GET")

        # 500 for the GET-based probes; lm_studio/comfyui still parse JSON so
        # give them benign shapes. A 500 -> "degraded" for qdrant/emb/voice.
        degraded = _Resp(500, {"data": [], "devices": [{}]})
        redis_mock = MagicMock()
        redis_mock.ping = AsyncMock()
        redis_mock.aclose = AsyncMock()

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.db.check_health", new=AsyncMock(return_value={"status": "ok"})), \
             patch("redis.asyncio.from_url", return_value=redis_mock), \
             patch("httpx.AsyncClient", return_value=_fake_async_client(get_resp=degraded)):
            result = await handler(_mk_request())

        assert result["subsystems"]["qdrant"]["status"] == "degraded"
        assert result["subsystems"]["embeddings"]["status"] == "degraded"
        assert result["subsystems"]["voice"]["status"] == "degraded"
        # No "down" present (lm_studio/comfyui returned 200-shaped JSON) but a
        # "degraded" exists -> overall "ok" only if no down; here none down.
        assert result["status"] == "ok"


# ═══════════════════════════════════════════════════════════════════════════
# TTS proxy (lines 289-300)
# ═══════════════════════════════════════════════════════════════════════════


class TestTTSProxy:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from app.routes import TTSRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/tts", "POST")
        with patch("app.routes._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(TTSRequest(text="hello"), _mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_400_when_text_is_only_actions(self):
        from app.routes import TTSRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/tts", "POST")
        # strip_actions_for_tts removes parentheticals -> empty -> 400.
        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")):
            resp = await handler(TTSRequest(text="(I smile softly)"), _mk_request())
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_returns_base64_audio_on_success(self):
        import base64
        from app.routes import TTSRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/tts", "POST")

        audio = b"\x00\x01RIFFWAVE"
        ctx = _fake_async_client(post_resp=_Resp(200, content=audio))

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes.httpx.AsyncClient", return_value=ctx):
            result = await handler(TTSRequest(text="Hello there.", language="ja"), _mk_request())

        assert result == {"audio": base64.b64encode(audio).decode()}
        # Voice service called with the cleaned, length-capped text + language.
        body = ctx._client.post.call_args.kwargs["json"]
        assert body["text"] == "Hello there."
        assert body["language"] == "ja"

    @pytest.mark.asyncio
    async def test_passes_upstream_error_status(self):
        from app.routes import TTSRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/tts", "POST")
        ctx = _fake_async_client(post_resp=_Resp(422, content=b""))
        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes.httpx.AsyncClient", return_value=ctx):
            resp = await handler(TTSRequest(text="speak"), _mk_request())
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_503_when_voice_unreachable(self):
        from app.routes import TTSRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/tts", "POST")
        ctx = _fake_async_client(post_exc=RuntimeError("connection refused"))
        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes.httpx.AsyncClient", return_value=ctx):
            resp = await handler(TTSRequest(text="speak"), _mk_request())
        assert resp.status_code == 503


# ═══════════════════════════════════════════════════════════════════════════
# generate-image (lines 312-319 success path)
# ═══════════════════════════════════════════════════════════════════════════


class TestGenerateImage:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from app.routes import ImageGenRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/generate-image", "POST")
        with patch("app.routes._get_user_id", new=AsyncMock(return_value=None)):
            resp = await handler(ImageGenRequest(prompt="klukai"), _mk_request(token=None))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_success_returns_image_and_archives(self):
        import base64
        from app.routes import ImageGenRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/generate-image", "POST")

        img = b"PNGDATA"
        save = AsyncMock(return_value="mem-42")
        state = _mk_aff_state(level=6)

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes.generate_image", new=AsyncMock(return_value=img)), \
             patch("app.routes.affection.get_state", new=AsyncMock(return_value=state)), \
             patch("app.routes.memory_archive.save_image", new=save):
            result = await handler(ImageGenRequest(prompt="klukai portrait"), _mk_request())

        assert result["image"] == base64.b64encode(img).decode()
        assert result["format"] == "png"
        assert result["memory_id"] == "mem-42"
        # Archived under the right user, prompt, source, and affection level.
        kwargs = save.call_args.kwargs
        assert save.call_args.args[1] == "klukai portrait"
        assert save.call_args.args[2] == "api"
        assert kwargs["affection_level"] == 6
        assert kwargs["user_id"] == "alice"

    @pytest.mark.asyncio
    async def test_500_when_generation_returns_none(self):
        from app.routes import ImageGenRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/generate-image", "POST")
        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes.generate_image", new=AsyncMock(return_value=None)):
            resp = await handler(ImageGenRequest(prompt="x"), _mk_request())
        assert resp.status_code == 500


# ═══════════════════════════════════════════════════════════════════════════
# Gift tiers + ws broadcast + audit (lines 343-345, 355-356, 366-367)
# ═══════════════════════════════════════════════════════════════════════════


class TestGiftTiersAndSideEffects:
    @pytest.mark.asyncio
    async def test_favoured_gift_gives_mid_bonus(self):
        from app.routes import GiftRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/gift", "POST")

        state = _mk_aff_state(score=300, level=3)
        personality = {
            "gift_preferences": {"favoured": ["coffee"]},
            "gift_reactions": {"favoured": "Thank you."},
        }
        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=False)

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes.affection.get_state", new=AsyncMock(return_value=state)), \
             patch("app.routes.affection._save_state", new=AsyncMock()), \
             patch("app.routes.load_personality", return_value=personality), \
             patch("app.routes.ws", ws_mock):
            result = await handler(GiftRequest(gift="coffee"), _mk_request())

        assert result["tier"] == "favoured"
        assert result["bonus"] == 5
        assert result["new_score"] == 305

    @pytest.mark.asyncio
    async def test_liked_gift_gives_small_bonus(self):
        from app.routes import GiftRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/gift", "POST")

        state = _mk_aff_state(score=300, level=3)
        personality = {
            "gift_preferences": {"liked": ["snack"]},
            "gift_reactions": {"liked": "Hm. Fine."},
        }
        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=False)

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes.affection.get_state", new=AsyncMock(return_value=state)), \
             patch("app.routes.affection._save_state", new=AsyncMock()), \
             patch("app.routes.load_personality", return_value=personality), \
             patch("app.routes.ws", ws_mock):
            result = await handler(GiftRequest(gift="snack"), _mk_request())

        assert result["tier"] == "liked"
        assert result["bonus"] == 2
        assert result["new_score"] == 302

    @pytest.mark.asyncio
    async def test_ws_broadcast_when_connected(self):
        """When the user has a live socket, the reaction + affection update are
        pushed (lines 355-356) and the gift is audit-logged (line 361)."""
        from app.routes import GiftRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/gift", "POST")

        state = _mk_aff_state(score=400, level=4)
        personality = {
            "gift_preferences": {"loved": ["roses"]},
            "gift_reactions": {"loved": "...You shouldn't have."},
        }
        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=True)
        ws_mock.send_proactive = AsyncMock()
        ws_mock.send_affection = AsyncMock()
        # The handler does `from . import audit` — that binds the already-loaded
        # app.audit module, so patch its attributes in place rather than swapping
        # sys.modules (which the package-attribute import would ignore).
        import app.audit as audit_mod
        audit_log = AsyncMock()

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes.affection.get_state", new=AsyncMock(return_value=state)), \
             patch("app.routes.affection._save_state", new=AsyncMock()), \
             patch("app.routes.load_personality", return_value=personality), \
             patch("app.routes.ws", ws_mock), \
             patch.object(audit_mod, "log", new=audit_log):
            result = await handler(GiftRequest(gift="roses"), _mk_request())

        assert result["tier"] == "loved"
        # Reaction pushed over the socket.
        ws_mock.send_proactive.assert_awaited_once()
        assert "shouldn't have" in ws_mock.send_proactive.call_args.args[1]
        # Affection delta broadcast with the +10 bonus.
        ws_mock.send_affection.assert_awaited_once()
        assert ws_mock.send_affection.call_args.args[4] == 10
        # Gift audited with tier + bonus metadata.
        audit_log.assert_awaited_once()
        meta = audit_log.call_args.kwargs["metadata"]
        assert meta == {"gift": "roses", "tier": "loved", "bonus": 10}
        assert audit_log.call_args.kwargs["user_id"] == "alice"

    @pytest.mark.asyncio
    async def test_audit_failure_is_swallowed(self):
        """A broken audit log must not fail the gift response (lines 366-367)."""
        from app.routes import GiftRequest
        app = _app_with_routes()
        handler = _find_route(app, "/api/gift", "POST")

        state = _mk_aff_state(score=400, level=4)
        personality = {
            "gift_preferences": {"loved": ["roses"]},
            "gift_reactions": {"loved": "Noted."},
        }
        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=False)
        import app.audit as audit_mod
        failing_log = AsyncMock(side_effect=RuntimeError("audit chain down"))

        with patch("app.routes._get_user_id", new=AsyncMock(return_value="alice")), \
             patch("app.routes.affection.get_state", new=AsyncMock(return_value=state)), \
             patch("app.routes.affection._save_state", new=AsyncMock()), \
             patch("app.routes.load_personality", return_value=personality), \
             patch("app.routes.ws", ws_mock), \
             patch.object(audit_mod, "log", new=failing_log):
            result = await handler(GiftRequest(gift="roses"), _mk_request())

        # Response still succeeds despite the audit exception.
        assert result["tier"] == "loved"
        assert result["new_score"] == 410


# ═══════════════════════════════════════════════════════════════════════════
# _run_mission background helper (lines 384-419)
# ═══════════════════════════════════════════════════════════════════════════


def _async_iter(tokens):
    async def _gen(*args, **kwargs):
        for t in tokens:
            yield t
    return _gen


class TestRunMission:
    @pytest.mark.asyncio
    async def test_warm_tone_at_high_affection_and_delivers_report(self):
        from app.routes import _run_mission

        captured = {}

        async def _stream(prompt, messages, config):
            captured["prompt"] = prompt
            for t in ["I ", "found ", "something."]:
                yield t

        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=True)
        ws_mock.send_proactive = AsyncMock()
        aff = _mk_aff_state(score=50, level=5)
        save = AsyncMock()

        with patch("app.routes.asyncio.sleep", new=AsyncMock()), \
             patch("random.choice", return_value="a data chip"), \
             patch("app.routes.router.route", new=AsyncMock(return_value=MagicMock())), \
             patch("app.routes.router.stream", new=_stream), \
             patch("app.routes.ws", ws_mock), \
             patch("app.routes.affection.get_state", new=AsyncMock(return_value=aff)), \
             patch("app.routes.affection._save_state", new=save):
            await _run_mission("alice", affection_level=5)

        # affection_level >= 3 -> warm tone instruction in the prompt.
        assert "Write warmly" in captured["prompt"]
        assert "a data chip" in captured["prompt"]
        # The streamed tokens are joined + narration-fixed and pushed.
        ws_mock.send_proactive.assert_awaited_once()
        report = ws_mock.send_proactive.call_args.args[1]
        assert "found" in report
        # Mission completion nudges affection upward and persists it.
        save.assert_awaited_once()
        assert aff.score == 53

    @pytest.mark.asyncio
    async def test_cold_tone_at_zero_affection(self):
        from app.routes import _run_mission
        captured = {}

        async def _stream(prompt, messages, config):
            captured["prompt"] = prompt
            yield "Sortie complete."

        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=False)

        with patch("app.routes.asyncio.sleep", new=AsyncMock()), \
             patch("random.choice", return_value="a comm device"), \
             patch("app.routes.router.route", new=AsyncMock(return_value=MagicMock())), \
             patch("app.routes.router.stream", new=_stream), \
             patch("app.routes.ws", ws_mock), \
             patch("app.routes.affection.get_state", new=AsyncMock(return_value=_mk_aff_state())), \
             patch("app.routes.affection._save_state", new=AsyncMock()):
            await _run_mission("bob", affection_level=0)

        assert "Write coldly" in captured["prompt"]
        # No socket -> nothing pushed.
        ws_mock.send_proactive.assert_not_called()

    @pytest.mark.asyncio
    async def test_professional_tone_at_low_affection(self):
        from app.routes import _run_mission
        captured = {}

        async def _stream(prompt, messages, config):
            captured["prompt"] = prompt
            yield "Patrol done."

        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=False)

        with patch("app.routes.asyncio.sleep", new=AsyncMock()), \
             patch("random.choice", return_value="a field ration set"), \
             patch("app.routes.router.route", new=AsyncMock(return_value=MagicMock())), \
             patch("app.routes.router.stream", new=_stream), \
             patch("app.routes.ws", ws_mock), \
             patch("app.routes.affection.get_state", new=AsyncMock(return_value=_mk_aff_state())), \
             patch("app.routes.affection._save_state", new=AsyncMock()):
            await _run_mission("carol", affection_level=1)

        assert "Write professionally" in captured["prompt"]

    @pytest.mark.asyncio
    async def test_falls_back_to_canned_report_on_llm_failure(self):
        """If router.route raises, the except branch (416-419) delivers a
        canned sortie report over the socket."""
        from app.routes import _run_mission

        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=True)
        ws_mock.send_proactive = AsyncMock()

        with patch("app.routes.asyncio.sleep", new=AsyncMock()), \
             patch("random.choice", return_value="a tactical flashlight"), \
             patch("app.routes.router.route",
                   new=AsyncMock(side_effect=RuntimeError("LLM down"))), \
             patch("app.routes.ws", ws_mock):
            await _run_mission("dave", affection_level=5)

        ws_mock.send_proactive.assert_awaited_once()
        fallback = ws_mock.send_proactive.call_args.args[1]
        assert "Sortie complete" in fallback
        assert "a tactical flashlight" in fallback

    @pytest.mark.asyncio
    async def test_empty_stream_uses_canned_debrief(self):
        """No tokens streamed -> the `if full else` fallback builds a default
        debrief mentioning the gift (line 408 false branch)."""
        from app.routes import _run_mission

        async def _stream(prompt, messages, config):
            return
            yield  # pragma: no cover — makes this an async generator

        ws_mock = MagicMock()
        ws_mock.is_connected = MagicMock(return_value=True)
        ws_mock.send_proactive = AsyncMock()

        with patch("app.routes.asyncio.sleep", new=AsyncMock()), \
             patch("random.choice", return_value="a signal relay component"), \
             patch("app.routes.router.route", new=AsyncMock(return_value=MagicMock())), \
             patch("app.routes.router.stream", new=_stream), \
             patch("app.routes.ws", ws_mock), \
             patch("app.routes.affection.get_state", new=AsyncMock(return_value=_mk_aff_state())), \
             patch("app.routes.affection._save_state", new=AsyncMock()):
            await _run_mission("erin", affection_level=5)

        report = ws_mock.send_proactive.call_args.args[1]
        assert "a signal relay component" in report
