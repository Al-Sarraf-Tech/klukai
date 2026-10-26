import base64
import json
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app import local_presence, voice_client
from app.llm_router import LLMRouter


def test_presence_requires_fresh_true_boolean(tmp_path):
    path = tmp_path / "presence.json"
    assert not local_presence.game_active(str(path))
    for body, expected in [
        ({"updated_at": time.time(), "game_active": True}, True),
        ({"updated_at": time.time() - 60, "game_active": True}, False),
        ({"updated_at": time.time() + 100, "game_active": True}, False),
        ({"updated_at": time.time(), "game_active": "false"}, False),
        ([], False),
    ]:
        path.write_text(json.dumps(body))
        assert local_presence.game_active(str(path)) is expected


async def test_gaming_without_lm_connection(monkeypatch, tmp_path):
    path = tmp_path / "presence.json"
    path.write_text(json.dumps({"updated_at": time.time(), "game_active": True}))
    monkeypatch.setenv("KLUKAI_GAME_STATUS_FILE", str(path))
    router = LLMRouter()
    router._lmstudio_available = False
    assert await router.is_game_active() is True


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("VOICE_BACKEND", "fish")
    monkeypatch.setenv("KLUKAI_VOICE_ENABLED", "true")
    monkeypatch.setenv("KLUKAI_FISH_STT_ENABLED", "true")
    monkeypatch.setenv("FISH_API_KEY", "test-only-key")


async def test_fish_transcription_contract(enabled):
    request = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"text": "Hello."},
            request=httpx.Request("POST", "https://api.fish.audio/v1/asr"),
        )
    )
    with patch("app.voice_client.http_request", request):
        result = await voice_client.post_fish_stt(
            base64.b64encode(b"RIFFtestWAVE").decode()
        )
    assert result.json() == {"text": "Hello."}
    kwargs = request.call_args.kwargs
    assert kwargs["headers"]["model"] == "transcribe-1-pro"
    assert kwargs["data"]["diarize"] == "false"
    assert kwargs["files"]["audio"][1] == b"RIFFtestWAVE"


@pytest.mark.parametrize("audio", ["", "not-base64!"])
async def test_invalid_audio_never_spends(enabled, audio):
    with patch("app.voice_client.http_request", side_effect=AssertionError("network")):
        assert (await voice_client.post_fish_stt(audio)).status_code == 400


async def test_off_never_spends(enabled, monkeypatch):
    monkeypatch.setenv("KLUKAI_FISH_STT_ENABLED", "false")
    with patch("app.voice_client.http_request", side_effect=AssertionError("network")):
        assert (await voice_client.post_fish_stt("YWJj")).status_code == 503


@pytest.mark.parametrize("status", [402, 429, 500])
async def test_fish_failures_do_not_echo_provider_details(enabled, status):
    request = AsyncMock(
        return_value=httpx.Response(status, json={"error": "private account detail"})
    )
    with patch("app.voice_client.http_request", request):
        result = await voice_client.post_fish_stt("YWJj")
    assert result.status_code == 502
    assert b"private" not in result.content
    assert request.await_count == 1


async def test_microphone_capabilities_are_authenticated(enabled):
    from fastapi import FastAPI
    from app.routes_voice_input import register_voice_input

    app = FastAPI()
    resolve = AsyncMock(return_value=None)
    register_voice_input(app, resolve)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/voice/capabilities")).status_code == 401
        resolve.return_value = "claude"
        assert (await client.get("/api/voice/capabilities")).json()[
            "input_enabled"
        ] is True
        with patch(
            "app.voice_client.post_fish_stt",
            AsyncMock(return_value=httpx.Response(200, json={"text": "Hello."})),
        ) as stt:
            r = await client.post("/api/stt", json={"audio": "YWJj"})
        assert r.json() == {"text": "Hello."}
        stt.assert_awaited_once_with("YWJj")


async def test_fish_websocket_uses_cloud_transcriber(enabled):
    from app import chat
    from app.models import SessionState

    with (
        patch(
            "app.voice_client.post_fish_stt",
            AsyncMock(
                return_value=httpx.Response(
                    200,
                    json={"text": "Hello."},
                    request=httpx.Request("POST", "https://api.fish.audio/v1/asr"),
                )
            ),
        ) as stt,
        patch.object(chat.ws, "send_thinking", AsyncMock()),
        patch.object(chat, "_handle_message", AsyncMock(return_value=None)) as message,
    ):
        await chat._handle_voice(
            "YWJj", SessionState(conversation_id="fish-stt-test"), "claude"
        )
    stt.assert_awaited_once_with("YWJj")
    assert message.call_args.args[0] == "Hello."


async def test_fish_transcription_preserves_request_for_status_checks(enabled):
    upstream = httpx.Response(
        200,
        json={"text": "Hello.", "provider_metadata": "not forwarded"},
        request=httpx.Request("POST", "https://api.fish.audio/v1/asr"),
    )
    with patch("app.voice_client.http_request", AsyncMock(return_value=upstream)):
        result = await voice_client.post_fish_stt("YWJj")
    assert result.json() == {"text": "Hello."}
    result.raise_for_status()
    assert result.request is upstream.request


async def test_fish_websocket_processes_actual_transcriber_result(enabled):
    from app import chat
    from app.models import SessionState

    upstream = httpx.Response(
        200,
        json={"text": "Hello."},
        request=httpx.Request("POST", "https://api.fish.audio/v1/asr"),
    )
    with (
        patch(
            "app.voice_client.http_request", AsyncMock(return_value=upstream)
        ) as request,
        patch.object(chat.ws, "send_thinking", AsyncMock()),
        patch.object(chat.ws, "send", AsyncMock()) as send,
        patch.object(chat, "_handle_message", AsyncMock(return_value=None)) as message,
    ):
        session = SessionState(conversation_id="fish-stt-contract")
        await chat._handle_voice("YWJj", session, "claude")
    request.assert_awaited_once()
    message.assert_awaited_once_with("Hello.", session, "claude")
    send.assert_not_awaited()
