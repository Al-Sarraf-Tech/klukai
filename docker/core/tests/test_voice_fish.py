"""The Fish Audio voice: free model only, taps only, English or Japanese.

No test reaches Fish or any LLM: ``http_request``, ``httpx.AsyncClient``, the
chat router and the database are all fakes.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app import voice_client, voice_language

SECRET = "fish-test-secret-key-do-not-log"
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32
VOICE_ID = "47fc35b7311944bc9fa238ed856de1f3"


@pytest.fixture
def fish(monkeypatch):
    monkeypatch.setenv("KLUKAI_VOICE_ENABLED", "true")
    monkeypatch.setenv("VOICE_BACKEND", "fish")
    monkeypatch.setenv("FISH_API_KEY", SECRET)
    monkeypatch.delenv("KLUKAI_FISH_MODEL", raising=False)
    monkeypatch.delenv("KLUKAI_FISH_VOICE_ID", raising=False)
    monkeypatch.setenv("VOICE_ASYNC_JOBS", "false")


class _Clients:
    """Stands in for ``httpx.AsyncClient``; records how each was built."""

    def __init__(self) -> None:
        self.built: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> MagicMock:
        self.built.append(kwargs)
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(side_effect=AssertionError("network used directly"))
        return client


def _fish_reply(status: int = 200, content: bytes = WAV, **kwargs: Any) -> AsyncMock:
    return AsyncMock(
        return_value=httpx.Response(
            status,
            content=content,
            request=httpx.Request("POST", voice_client.FISH_TTS_URL),
            **kwargs,
        )
    )


def _request() -> MagicMock:
    req = MagicMock()
    req.headers = {"Authorization": "Bearer good"}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    return req


def _route(path: str, method: str):
    from app.routes import register_routes

    app = FastAPI()
    register_routes(app)
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(
            route, "methods", set()
        ):
            return route.endpoint, app
    raise AssertionError(f"route not found: {method} {path}")


# ── The Fish request ──────────────────────────────────────────────────────


class TestFishRequest:
    async def test_payload_headers_and_wav_contract(self, fish):
        clients = _Clients()
        reply = _fish_reply()
        with patch("app.voice_client.http_request", reply), patch(
            "httpx.AsyncClient", clients
        ):
            response = await voice_client.post_leased_tts(
                MagicMock(), {"text": "Good evening, Commander.", "language": "en"}
            )

        assert response.status_code == 200
        assert response.content == WAV
        assert response.headers["content-type"] == "audio/wav"
        # The returned response never carries the credential.
        assert "authorization" not in response.request.headers

        reply.assert_awaited_once()
        dep, _client, method, url = reply.await_args.args
        assert (dep, method, url) == ("voice", "post", "https://api.fish.audio/v1/tts")
        kwargs = reply.await_args.kwargs
        assert kwargs["json"] == {
            "text": "Good evening, Commander.",
            "reference_id": VOICE_ID,
            "format": "wav",
            "sample_rate": 24000,
            "latency": "normal",
            "language": "en",
        }
        assert kwargs["headers"] == {
            "Authorization": f"Bearer {SECRET}",
            "model": "s2.1-pro-free",
        }
        # A dedicated client: no proxies from the environment, no redirects.
        assert clients.built == [
            {"timeout": 60.0, "trust_env": False, "follow_redirects": False}
        ]

    async def test_model_header_is_always_sent_and_overridable(self, fish, monkeypatch):
        monkeypatch.setenv("KLUKAI_FISH_MODEL", "s3-mini-free")
        monkeypatch.setenv("KLUKAI_FISH_VOICE_ID", "other-voice")
        reply = _fish_reply()
        with patch("app.voice_client.http_request", reply), patch(
            "httpx.AsyncClient", _Clients()
        ):
            await voice_client.post_leased_tts(MagicMock(), {"text": "hello"})
        assert reply.await_args.kwargs["headers"]["model"] == "s3-mini-free"
        assert reply.await_args.kwargs["json"]["reference_id"] == "other-voice"

    @pytest.mark.parametrize(
        ("given", "sent"), [("ja", "ja"), ("en", "en"), (None, "en"), ("fr", "en")]
    )
    async def test_language_is_en_or_ja(self, fish, given, sent):
        reply = _fish_reply()
        payload: dict[str, Any] = {"text": "はい"}
        if given is not None:
            payload["language"] = given
        with patch("app.voice_client.http_request", reply), patch(
            "httpx.AsyncClient", _Clients()
        ):
            await voice_client.post_leased_tts(MagicMock(), payload)
        assert reply.await_args.kwargs["json"]["language"] == sent

    async def test_deadline_is_capped(self, fish):
        reply = _fish_reply()
        clients = _Clients()
        with patch("app.voice_client.http_request", reply), patch(
            "httpx.AsyncClient", clients
        ):
            await voice_client.post_leased_tts(
                MagicMock(), {"text": "hello"}, timeout=600.0
            )
        assert reply.await_args.kwargs["timeout"] == 60.0
        assert clients.built[0]["timeout"] == 60.0

    async def test_no_lm_gate_and_no_gpu_lease(self, fish):
        with patch("app.voice_client.http_request", _fish_reply()), patch(
            "httpx.AsyncClient", _Clients()
        ), patch(
            "app.voice_client.gpu_lease", side_effect=AssertionError("GPU lease taken")
        ), patch(
            "app.llm_router.get_lm_gate", side_effect=AssertionError("LM gate taken")
        ):
            response = await voice_client.post_leased_tts(MagicMock(), {"text": "hi"})
        assert response.status_code == 200

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("FISH_API_KEY", ""),
            ("FISH_API_KEY", "   "),
            ("KLUKAI_FISH_MODEL", ""),
            ("KLUKAI_FISH_VOICE_ID", ""),
        ],
    )
    async def test_missing_configuration_is_refused(self, fish, monkeypatch, name, value):
        monkeypatch.setenv(name, value)
        reply = AsyncMock(side_effect=AssertionError("Fish contacted"))
        with patch("app.voice_client.http_request", reply), patch(
            "httpx.AsyncClient", side_effect=AssertionError("client built")
        ):
            response = await voice_client.post_leased_tts(MagicMock(), {"text": "hi"})
        assert response.status_code == 503
        assert response.json() == {"error": "Voice is not configured"}

    async def test_unset_key_is_refused(self, fish, monkeypatch):
        monkeypatch.delenv("FISH_API_KEY")
        with patch(
            "app.voice_client.http_request",
            AsyncMock(side_effect=AssertionError("Fish contacted")),
        ):
            response = await voice_client.post_leased_tts(MagicMock(), {"text": "hi"})
        assert response.status_code == 503

    @pytest.mark.parametrize("model", ["s2.1-pro", "s1", "s2.1-pro-free-trial"])
    async def test_a_paid_model_is_never_requested(self, fish, monkeypatch, model):
        monkeypatch.setenv("KLUKAI_FISH_MODEL", model)
        with patch(
            "app.voice_client.http_request",
            AsyncMock(side_effect=AssertionError("Fish contacted")),
        ):
            response = await voice_client.post_leased_tts(MagicMock(), {"text": "hi"})
        assert response.status_code == 503

    async def test_empty_text_is_not_sent(self, fish):
        with patch(
            "app.voice_client.http_request",
            AsyncMock(side_effect=AssertionError("Fish contacted")),
        ):
            response = await voice_client.post_leased_tts(MagicMock(), {"text": "  "})
        assert response.status_code == 400

    @pytest.mark.parametrize("status", [400, 401, 402, 403, 429, 500, 503])
    async def test_non_200_maps_to_502_without_the_body(self, fish, status):
        upstream = b'{"message": "insufficient balance for account acct_123"}'
        with patch(
            "app.voice_client.http_request", _fish_reply(status, upstream)
        ), patch("httpx.AsyncClient", _Clients()):
            response = await voice_client.post_leased_tts(MagicMock(), {"text": "hi"})
        assert response.status_code == 502
        assert response.json() == {"error": "Voice service unavailable"}
        assert b"acct_123" not in response.content
        # Callers may raise_for_status(); it must raise, not crash on no request.
        with pytest.raises(httpx.HTTPStatusError):
            response.raise_for_status()

    @pytest.mark.parametrize(
        "content",
        [b"", b'{"error": "oops"}', b"RIFF\x00\x00\x00\x00AVI LIST"],
        ids=["empty", "json", "not-wave"],
    )
    async def test_a_200_that_is_not_wav_is_rejected(self, fish, content):
        with patch(
            "app.voice_client.http_request", _fish_reply(200, content)
        ), patch("httpx.AsyncClient", _Clients()):
            with pytest.raises(ValueError, match="Invalid voice audio"):
                await voice_client.post_leased_tts(MagicMock(), {"text": "hi"})

    async def test_oversized_audio_is_rejected(self, fish, monkeypatch):
        monkeypatch.setattr(voice_client, "MAX_AUDIO_BYTES", len(WAV) - 1)
        with patch("app.voice_client.http_request", _fish_reply()), patch(
            "httpx.AsyncClient", _Clients()
        ):
            with pytest.raises(ValueError, match="Invalid voice audio"):
                await voice_client.post_leased_tts(MagicMock(), {"text": "hi"})

    async def test_master_switch_still_rules(self, fish, monkeypatch):
        monkeypatch.setenv("KLUKAI_VOICE_ENABLED", "false")
        with patch(
            "app.voice_client.http_request",
            AsyncMock(side_effect=AssertionError("Fish contacted")),
        ):
            response = await voice_client.post_leased_tts(MagicMock(), {"text": "hi"})
        assert response.status_code == 503
        assert response.json() == {"error": "Voice is turned off"}

    async def test_the_key_is_never_logged(self, fish, monkeypatch, caplog):
        caplog.set_level(logging.DEBUG)
        for reply in (
            _fish_reply(),
            _fish_reply(401, b"bad key " + SECRET.encode()),
            _fish_reply(200, b"not audio"),
            AsyncMock(side_effect=httpx.ConnectError("boom")),
        ):
            with patch("app.voice_client.http_request", reply), patch(
                "httpx.AsyncClient", _Clients()
            ):
                try:
                    response = await voice_client.post_leased_tts(
                        MagicMock(), {"text": "hi"}
                    )
                except (ValueError, httpx.HTTPError):
                    continue
                assert SECRET.encode() not in response.content
        monkeypatch.setenv("KLUKAI_FISH_MODEL", "s2.1-pro")
        await voice_client.post_leased_tts(MagicMock(), {"text": "hi"})
        assert caplog.records, "expected the refusals to be logged"
        assert SECRET not in caplog.text


class TestBackendSwitches:
    def test_fish_turns_off_input_and_letters_only(self, fish):
        assert voice_client.fish_voice_enabled() is True
        assert voice_client.voice_enabled() is True
        assert voice_client.voice_input_enabled() is False
        assert voice_client.voice_letters_enabled() is False
        assert voice_client.local_voice_enabled() is False

    @pytest.mark.parametrize("backend", ["dominus", "amarillo-xpu"])
    def test_other_backends_keep_input_and_letters(self, monkeypatch, backend):
        monkeypatch.setenv("KLUKAI_VOICE_ENABLED", "true")
        monkeypatch.setenv("VOICE_BACKEND", backend)
        assert voice_client.fish_voice_enabled() is False
        assert voice_client.voice_input_enabled() is True
        assert voice_client.voice_letters_enabled() is True

    def test_master_switch_turns_everything_off(self, fish, monkeypatch):
        monkeypatch.setenv("KLUKAI_VOICE_ENABLED", "false")
        assert voice_client.voice_input_enabled() is False
        assert voice_client.voice_letters_enabled() is False


# ── The per-Commander setting ─────────────────────────────────────────────


def _fake_conn(row: Any = None, *, fail: bool = False):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone = AsyncMock(return_value=row)
    conn.execute = AsyncMock(
        side_effect=RuntimeError("db down") if fail else None, return_value=cursor
    )

    @asynccontextmanager
    async def factory():
        yield conn

    return factory, conn


class TestSettingStore:
    @pytest.mark.parametrize(
        ("row", "expected"),
        [(("ja",), "ja"), (("en",), "en"), (None, "en"), (("xx",), "en")],
    )
    async def test_load(self, row, expected):
        factory, conn = _fake_conn(row)
        with patch("app.voice_language.get_conn", factory):
            assert await voice_language.load_voice_language("claude") == expected
        sql, params = conn.execute.await_args.args
        assert "companion_user_settings" in sql
        assert params == ("claude",)

    async def test_load_raises_but_speech_falls_back_to_english(self):
        factory, _ = _fake_conn(fail=True)
        with patch("app.voice_language.get_conn", factory):
            with pytest.raises(RuntimeError):
                await voice_language.load_voice_language("claude")
            assert await voice_language.voice_language_for("claude") == "en"

    async def test_save_upserts(self):
        factory, conn = _fake_conn()
        with patch("app.voice_language.get_conn_autocommit", factory):
            assert await voice_language.save_voice_language("claude", "ja") == "ja"
        sql, params = conn.execute.await_args.args
        assert "INSERT INTO companion_user_settings" in sql
        assert "ON CONFLICT (user_id) DO UPDATE" in sql
        assert params == ("claude", "ja")

    async def test_save_refuses_other_languages(self):
        factory, conn = _fake_conn()
        with patch("app.voice_language.get_conn_autocommit", factory):
            with pytest.raises(ValueError):
                await voice_language.save_voice_language("claude", "fr")
        conn.execute.assert_not_awaited()

    def test_migration_defines_the_table(self):
        from pathlib import Path

        sql = (
            Path(__file__).resolve().parent.parent
            / "migrations"
            / "240_user_settings.sql"
        ).read_text()
        assert "CREATE TABLE IF NOT EXISTS companion_user_settings" in sql
        assert "CHECK (voice_language IN ('en', 'ja'))" in sql


class TestSettingRoutes:
    @staticmethod
    def _app(user: str | None = "claude") -> FastAPI:
        from app.routes_voice_settings import register_voice_settings

        async def resolve(request):
            if request.headers.get("Authorization") == "Bearer good":
                return user
            return None

        app = FastAPI()
        register_voice_settings(app, resolve)
        return app

    @staticmethod
    async def _call(app: FastAPI, method: str, **kwargs: Any) -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://core.test"
        ) as client:
            return await client.request(method, "/api/voice/settings", **kwargs)

    @pytest.mark.parametrize("method", ["GET", "PUT"])
    async def test_login_required(self, method):
        load = AsyncMock(side_effect=AssertionError("read"))
        save = AsyncMock(side_effect=AssertionError("written"))
        with patch("app.voice_language.load_voice_language", load), patch(
            "app.voice_language.save_voice_language", save
        ):
            response = await self._call(
                self._app(), method, json={"language": "ja"}
            )
        assert response.status_code == 401

    async def test_get_returns_his_choice(self):
        with patch(
            "app.voice_language.load_voice_language", AsyncMock(return_value="ja")
        ) as load:
            response = await self._call(
                self._app(), "GET", headers={"Authorization": "Bearer good"}
            )
        assert response.status_code == 200
        assert response.json() == {"language": "ja", "languages": ["en", "ja"]}
        assert response.headers["cache-control"] == "no-store"
        load.assert_awaited_once_with("claude")

    @pytest.mark.parametrize("language", ["en", "ja"])
    async def test_put_saves_his_choice(self, language):
        with patch(
            "app.voice_language.save_voice_language",
            AsyncMock(return_value=language),
        ) as save:
            response = await self._call(
                self._app(),
                "PUT",
                headers={"Authorization": "Bearer good"},
                json={"language": language},
            )
        assert response.status_code == 200
        assert response.json()["language"] == language
        save.assert_awaited_once_with("claude", language)

    @pytest.mark.parametrize("body", [{"language": "fr"}, {}, {"language": None}])
    async def test_put_rejects_other_languages(self, body):
        with patch(
            "app.voice_language.save_voice_language",
            AsyncMock(side_effect=AssertionError("written")),
        ):
            response = await self._call(
                self._app(),
                "PUT",
                headers={"Authorization": "Bearer good"},
                json=body,
            )
        assert response.status_code == 422

    async def test_database_outage_is_503(self):
        boom = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("app.voice_language.load_voice_language", boom), patch(
            "app.voice_language.save_voice_language", boom
        ):
            got = await self._call(
                self._app(), "GET", headers={"Authorization": "Bearer good"}
            )
            put = await self._call(
                self._app(),
                "PUT",
                headers={"Authorization": "Bearer good"},
                json={"language": "ja"},
            )
        assert got.status_code == put.status_code == 503

    def test_schema(self):
        from app.api_schemas import VoiceSettingsRequest

        assert VoiceSettingsRequest(language="ja").language == "ja"
        with pytest.raises(ValidationError):
            VoiceSettingsRequest(language="de")

    def test_registered_on_the_real_app(self):
        from app.routes import register_routes

        app = FastAPI()
        register_routes(app)
        methods = {
            m
            for r in app.routes
            if getattr(r, "path", None) == "/api/voice/settings"
            for m in r.methods
        }
        assert {"GET", "PUT"} <= methods


# ── Japanese through the chat LLM ─────────────────────────────────────────


class _Router:
    """The chat router's ``route`` + ``stream`` surface."""

    def __init__(self, tokens: list[str] | None = None, *, delay: float = 0.0):
        self.tokens = tokens if tokens is not None else ["はい、", "指揮官。"]
        self.delay = delay
        self.route = AsyncMock(return_value=MagicMock(provider="anthropic"))
        self.calls: list[tuple[str, list[dict[str, Any]], Any]] = []

    async def stream(self, system, messages, config):
        self.calls.append((system, messages, config))
        if self.delay:
            await asyncio.sleep(self.delay)
        for token in self.tokens:
            yield token


@pytest.fixture
def redis_cache(fake_redis, monkeypatch):
    from app import caches

    async def _get():
        return fake_redis

    monkeypatch.setattr(caches, "_get", _get)
    return fake_redis


class TestTranslation:
    async def test_uses_the_interactive_chat_path(self, redis_cache):
        router = _Router()
        with patch("app.context.router", router):
            result = await voice_language.translate_to_japanese("Yes, Commander.")
        assert result == "はい、指揮官。"
        router.route.assert_awaited_once()
        assert router.route.await_args.kwargs == {"interactive": True}
        system, messages, _config = router.calls[0]
        assert system == voice_language.TRANSLATION_PROMPT
        assert messages == [{"role": "user", "content": "Yes, Commander."}]

    def test_prompt_sets_her_register(self):
        prompt = voice_language.TRANSLATION_PROMPT
        for needle in ("です/ます", "Elegant, calm and confident", "ヨンマルヨン", "指揮官"):
            assert needle in prompt

    async def test_a_replay_is_served_from_cache(self, redis_cache):
        router = _Router()
        with patch("app.context.router", router):
            first = await voice_language.translate_to_japanese("Yes, Commander.")
            second = await voice_language.translate_to_japanese("Yes, Commander.")
        assert first == second == "はい、指揮官。"
        assert len(router.calls) == 1
        assert any(key.startswith("cache:voice:ja:") for key in redis_cache._store)

    async def test_cache_write_has_a_ttl(self):
        from app import caches

        redis = MagicMock()
        redis.set = AsyncMock()

        async def _get():
            return redis

        with patch.object(caches, "_get", _get):
            await caches.put_voice_translation("Yes.", "はい。")
        assert redis.set.await_args.kwargs["ex"] == caches.VOICE_TRANSLATION_TTL

    async def test_redis_outage_still_translates(self, monkeypatch):
        from app import caches

        async def _down():
            raise ConnectionError("redis down")

        monkeypatch.setattr(caches, "_get", _down)
        with patch("app.context.router", _Router()):
            assert await voice_language.translate_to_japanese("Yes.") == "はい、指揮官。"

    @pytest.mark.parametrize(
        ("raw", "spoken"),
        [
            ("「（微笑む）ヨンマルヨンの任務です。」", "ヨンマルヨンの任務です。"),
            ("404小隊、出撃します。", "ヨンマルヨン小隊、出撃します。"),
            ("４０４です。", "ヨンマルヨンです。"),
            ("番号は14045です。", "番号は14045です。"),
            ("*nods* 了解しました、指揮官。", "了解しました、指揮官。"),
            ("<think>plan</think>```\nはい。\n```", "はい。"),
        ],
    )
    async def test_output_is_cleaned(self, redis_cache, raw, spoken):
        with patch("app.context.router", _Router([raw])):
            assert await voice_language.translate_to_japanese("line") == spoken

    @pytest.mark.parametrize(
        "tokens",
        [
            ["Communications disrupted, Commander. Standby for reconnection."],
            ["Yes, Commander."],
            [""],
            [],
        ],
        ids=["sentinel", "english", "blank", "nothing"],
    )
    async def test_no_japanese_falls_back_to_english(self, redis_cache, tokens):
        with patch("app.context.router", _Router(tokens)):
            assert await voice_language.translate_to_japanese("Yes.") is None
            assert await voice_language.speech_for("Yes.", "ja") == ("Yes.", "en")
        assert redis_cache._store == {}

    async def test_router_failure_falls_back_to_english(self, redis_cache):
        router = _Router()
        router.route = AsyncMock(side_effect=RuntimeError("No local LLM backend"))
        with patch("app.context.router", router):
            assert await voice_language.speech_for("Yes.", "ja") == ("Yes.", "en")

    async def test_slow_translation_falls_back_to_english(
        self, redis_cache, monkeypatch
    ):
        monkeypatch.setattr(voice_language, "TRANSLATION_TIMEOUT", 0.01)
        with patch("app.context.router", _Router(delay=1.0)):
            assert await voice_language.speech_for("Yes.", "ja") == ("Yes.", "en")

    async def test_english_never_calls_the_llm(self):
        router = _Router()
        with patch("app.context.router", router):
            assert await voice_language.speech_for("Yes.", "en") == ("Yes.", "en")
        router.route.assert_not_awaited()

    async def test_japanese_speech(self, redis_cache):
        with patch("app.context.router", _Router()):
            assert await voice_language.speech_for("Yes.", "ja") == (
                "はい、指揮官。",
                "ja",
            )


# ── /api/tts with the Fish voice ──────────────────────────────────────────


class TestApiTts:
    @staticmethod
    async def _speak(text: str, language: str = "en"):
        from app.api_schemas import TTSRequest

        handler, app = _route("/api/tts", "POST")
        synth = AsyncMock(
            return_value=httpx.Response(
                200, content=WAV, headers={"Content-Type": "audio/wav"}
            )
        )
        with patch(
            "app.routes._get_user_id", new=AsyncMock(return_value="claude")
        ), patch("app.voice_client.post_leased_tts", synth):
            result = await handler(TTSRequest(text=text, language=language), _request())
        return result, synth, app

    async def test_english_setting_speaks_as_written(self, fish):
        with patch(
            "app.voice_language.voice_language_for", AsyncMock(return_value="en")
        ), patch(
            "app.voice_language.translate_to_japanese",
            AsyncMock(side_effect=AssertionError("translated")),
        ):
            result, synth, _ = await self._speak("(smiles) Good evening, Commander.")
        assert base64.b64decode(result["audio"]) == WAV
        assert synth.await_args.args[1] == {
            "text": "Good evening, Commander.",
            "language": "en",
        }

    async def test_japanese_setting_translates_first(self, fish):
        translate = AsyncMock(return_value="こんばんは、指揮官。")
        with patch(
            "app.voice_language.voice_language_for", AsyncMock(return_value="ja")
        ) as setting, patch("app.voice_language.translate_to_japanese", translate):
            result, synth, _ = await self._speak("*bows* Good evening, Commander.")
        setting.assert_awaited_once_with("claude")
        translate.assert_awaited_once_with("Good evening, Commander.")
        assert synth.await_args.args[1] == {
            "text": "こんばんは、指揮官。",
            "language": "ja",
        }
        assert "audio" in result

    async def test_failed_translation_speaks_english(self, fish):
        with patch(
            "app.voice_language.voice_language_for", AsyncMock(return_value="ja")
        ), patch(
            "app.voice_language.translate_to_japanese", AsyncMock(return_value=None)
        ):
            result, synth, _ = await self._speak("Good evening.")
        assert synth.await_args.args[1] == {"text": "Good evening.", "language": "en"}
        assert "audio" in result

    async def test_japanese_text_is_spoken_as_written(self, fish):
        with patch(
            "app.voice_language.voice_language_for",
            AsyncMock(side_effect=AssertionError("setting read")),
        ):
            _result, synth, _ = await self._speak("はい、指揮官。", language="ja")
        assert synth.await_args.args[1] == {"text": "はい、指揮官。", "language": "ja"}

    async def test_other_text_languages_are_refused(self, fish):
        result, synth, _ = await self._speak("Bonjour.", language="fr")
        assert result.status_code == 400
        synth.assert_not_awaited()

    async def test_queued_jobs_carry_the_translated_line(self, fish, monkeypatch):
        from app.api_schemas import TTSRequest

        monkeypatch.setenv("VOICE_ASYNC_JOBS", "true")
        handler, app = _route("/api/tts", "POST")
        app.state.voice_jobs.submit = MagicMock(
            return_value={"job_id": "j1", "status": "queued"}
        )
        with patch(
            "app.routes._get_user_id", new=AsyncMock(return_value="claude")
        ), patch(
            "app.voice_language.voice_language_for", AsyncMock(return_value="ja")
        ), patch(
            "app.voice_language.translate_to_japanese",
            AsyncMock(return_value="はい。"),
        ):
            response = await handler(TTSRequest(text="Yes."), _request())
        assert response.status_code == 202
        app.state.voice_jobs.submit.assert_called_once_with(
            "claude", {"text": "はい。", "language": "ja"}
        )

    async def test_upstream_failure_is_502_not_401(self, fish):
        from app.api_schemas import TTSRequest

        handler, _ = _route("/api/tts", "POST")
        with patch(
            "app.routes._get_user_id", new=AsyncMock(return_value="claude")
        ), patch(
            "app.voice_language.voice_language_for", AsyncMock(return_value="en")
        ), patch(
            "app.voice_client.http_request", _fish_reply(401, b"bad key")
        ), patch("httpx.AsyncClient", _Clients()):
            response = await handler(TTSRequest(text="Hello."), _request())
        assert response.status_code == 502
        assert b"bad key" not in response.body

    async def test_unconfigured_fish_is_not_a_success(self, fish, monkeypatch):
        from app.api_schemas import TTSRequest

        monkeypatch.setenv("FISH_API_KEY", "")
        handler, _ = _route("/api/tts", "POST")
        with patch(
            "app.routes._get_user_id", new=AsyncMock(return_value="claude")
        ), patch(
            "app.voice_language.voice_language_for", AsyncMock(return_value="en")
        ), patch(
            "app.voice_client.http_request",
            AsyncMock(side_effect=AssertionError("Fish contacted")),
        ):
            response = await handler(TTSRequest(text="Hello."), _request())
        assert response.status_code == 502


# ── Taps only: no microphone, no letters that play themselves ─────────────


class TestTapsOnly:
    async def test_stt_is_off(self, fish):
        from app.api_schemas import STTRequest

        handler, _ = _route("/api/stt", "POST")
        with patch(
            "app.routes_extras._get_user_id", new=AsyncMock(return_value="claude")
        ), patch("httpx.AsyncClient", side_effect=AssertionError("network")):
            response = await handler(STTRequest(audio="AAAA"), _request())
        assert response.status_code == 503
        assert response.body == b'{"error":"Voice input is off"}'

    async def test_stt_still_requires_a_login(self, fish):
        from app.api_schemas import STTRequest

        handler, _ = _route("/api/stt", "POST")
        with patch("app.routes_extras._get_user_id", new=AsyncMock(return_value=None)):
            response = await handler(STTRequest(audio="AAAA"), _request())
        assert response.status_code == 401

    async def test_spoken_chat_gets_an_error_frame(self, fish):
        from app.chat import _handle_voice
        from app.models import SessionState

        ws = MagicMock()
        ws.send = AsyncMock()
        ws.send_thinking = AsyncMock()
        ws.send_voice = AsyncMock()
        with patch("app.chat.ws", ws), patch(
            "httpx.AsyncClient", side_effect=AssertionError("network")
        ), patch(
            "app.chat._handle_message",
            new=AsyncMock(side_effect=AssertionError("replied")),
        ):
            await _handle_voice("AAAA", SessionState(conversation_id="c1"), "claude")
        ws.send_thinking.assert_not_called()
        ws.send_voice.assert_not_called()
        frame = ws.send.await_args.args[1]
        assert frame == {
            "type": "error",
            "message": "...Voice input is off for now, Commander. Type to me instead.",
        }

    @staticmethod
    def _letter_would_succeed(tmp_path, monkeypatch):
        """Everything a voice letter needs works, so only the guard stops it."""
        from app import voice_archive

        monkeypatch.setattr(voice_archive, "AUDIO_DIR", tmp_path)
        conn = MagicMock()
        conn.execute = AsyncMock()
        conn.commit = AsyncMock()

        @asynccontextmanager
        async def connection():
            yield conn

        pool = MagicMock()
        pool.connection = connection
        monkeypatch.setattr(voice_archive, "get_pool", lambda: pool)
        return AsyncMock(
            return_value=httpx.Response(
                200, content=WAV, headers={"Content-Type": "audio/wav"}
            )
        )

    async def test_voice_letter_is_not_synthesized(self, fish, tmp_path, monkeypatch):
        from app.voice_archive import save_voice_note

        synth = self._letter_would_succeed(tmp_path, monkeypatch)
        with patch("app.voice_client.post_leased_tts", synth):
            assert await save_voice_note("I missed you.", user_id="claude") is None
        synth.assert_not_awaited()
        assert list(tmp_path.iterdir()) == []

    async def test_the_guard_is_what_stops_it(self, tmp_path, monkeypatch):
        """Control: the same setup on Dominus does save a letter."""
        from app.voice_archive import save_voice_note

        monkeypatch.setenv("KLUKAI_VOICE_ENABLED", "true")
        monkeypatch.setenv("VOICE_BACKEND", "dominus")
        synth = self._letter_would_succeed(tmp_path, monkeypatch)
        with patch("app.voice_client.post_leased_tts", synth):
            assert await save_voice_note("I missed you.", user_id="claude")
        synth.assert_awaited_once()

    async def test_return_greeting_arrives_as_text_without_voice_id(
        self, fish, tmp_path, monkeypatch
    ):
        from app.affection import AffectionState
        from app.reflect_helpers import _deliver_return_greeting

        synth = self._letter_would_succeed(tmp_path, monkeypatch)
        ws = MagicMock()
        ws.is_connected = MagicMock(return_value=True)
        ws.send = AsyncMock()
        ws.send_proactive = AsyncMock()
        with patch("app.reflect_helpers.ws", ws), patch(
            "app.voice_client.post_leased_tts", synth
        ):
            await _deliver_return_greeting(
                "claude",
                "Welcome back, Commander.",
                "tender",
                AffectionState(),
                10.0,
                "reflection",
            )
        synth.assert_not_awaited()
        ws.send_proactive.assert_awaited_once_with(
            "claude", "Welcome back, Commander."
        )
        for call in ws.send.await_args_list:
            frame = call.args[1]
            assert frame.get("type") != "voice_letter"
            assert "voice_id" not in frame

    async def test_letters_still_synthesize_on_other_backends(self, monkeypatch):
        """The Fish guard is scoped: Dominus letters keep their audio."""
        from app import voice_archive

        monkeypatch.setenv("KLUKAI_VOICE_ENABLED", "true")
        monkeypatch.setenv("VOICE_BACKEND", "dominus")
        synth = AsyncMock(side_effect=RuntimeError("voice host down"))
        with patch("app.voice_client.post_leased_tts", synth):
            assert await voice_archive.save_voice_note("Hi.", user_id="claude") is None
        synth.assert_awaited_once()


# ── Health ────────────────────────────────────────────────────────────────


async def test_health_reports_fish_without_calling_it(fish):
    handler, _ = _route("/api/health/subsystems", "GET")
    probed: list[str] = []

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            probed.append(url)
            return httpx.Response(
                200, json={"data": []}, request=httpx.Request("GET", url)
            )

    redis = MagicMock()
    redis.ping = AsyncMock()
    redis.aclose = AsyncMock()
    with patch("app.routes._get_user_id", new=AsyncMock(return_value="claude")), patch(
        "app.db.check_health", new=AsyncMock(return_value={"status": "ok"})
    ), patch("redis.asyncio.from_url", return_value=redis), patch(
        "app.routes_health._image_subsystems",
        new=AsyncMock(return_value={"images": {"status": "ok"}}),
    ), patch("httpx.AsyncClient", _Client):
        result = await handler(_request())
    assert result["subsystems"]["voice"] == {"status": "ok", "backend": "fish"}
    assert not [url for url in probed if "fish.audio" in url or ":8301" in url]
