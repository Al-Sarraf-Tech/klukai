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
    assert await router.is_game_active() is True


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("KLUKAI_VOICE_ENABLED", "true")
    monkeypatch.setenv("KLUKAI_FISH_STT_ENABLED", "true")
    monkeypatch.setenv("FISH_API_KEY", "test-only-key")


@pytest.fixture
def fish_reply():
    from contextlib import asynccontextmanager

    calls = []

    @asynccontextmanager
    async def stream(*args, **kwargs):
        calls.append((args, kwargs))
        yield httpx.Response(
            200,
            json={"text": "Hello.", "provider_metadata": "not forwarded"},
            request=httpx.Request("POST", "https://api.fish.audio/v1/asr"),
        )

    with patch("app.voice_client.http_stream", stream):
        yield calls


async def test_microphone_capabilities_and_actual_cloud_transcriber(
    enabled, fish_reply
):
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
        result = await client.post("/api/stt", json={"audio": "YWJj"})
        assert result.status_code == 200
        assert result.json() == {"text": "Hello."}
    assert len(fish_reply) == 1
    args, kwargs = fish_reply[0]
    assert args[3] == "https://api.fish.audio/v1/asr"
    assert kwargs["headers"]["model"] == "transcribe-1-pro"
    assert kwargs["files"]["audio"][1] == b"abc"


async def test_fish_websocket_processes_actual_transcriber_result(enabled, fish_reply):
    from app import chat
    from app.models import SessionState

    with (
        patch.object(chat.ws, "send_thinking", AsyncMock()),
        patch.object(chat.ws, "send", AsyncMock()) as send,
        patch.object(chat, "_handle_message", AsyncMock(return_value=None)) as message,
    ):
        session = SessionState(conversation_id="fish-stt-contract")
        await chat._handle_voice("YWJj", session, "claude")
    assert len(fish_reply) == 1
    message.assert_awaited_once_with("Hello.", session, "claude")
    send.assert_not_awaited()


async def test_disabled_input_never_contacts_fish(enabled, monkeypatch, fish_reply):
    monkeypatch.setenv("KLUKAI_FISH_STT_ENABLED", "false")
    assert not voice_client.voice_input_enabled()
    with pytest.raises(voice_client.SpeechToTextError) as failed:
        await voice_client.transcribe("YWJj")
    assert failed.value.status == 503
    assert fish_reply == []
