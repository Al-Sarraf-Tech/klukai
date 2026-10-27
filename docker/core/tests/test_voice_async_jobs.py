"""Opt-in queued speech (``VOICE_ASYNC_JOBS``) on the Fish voice.

The real routes, job queue and Fish client run; only the network is a
``httpx.MockTransport`` standing in for ``api.fish.audio``.
"""

import asyncio
import base64
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32


def _app(monkeypatch, *, async_jobs="true"):
    from app.routes import register_routes

    monkeypatch.setenv("KLUKAI_VOICE_ENABLED", "true")
    monkeypatch.setenv("FISH_API_KEY", "pytest-fish-key")
    monkeypatch.delenv("KLUKAI_FISH_MODEL", raising=False)
    monkeypatch.delenv("KLUKAI_FISH_VOICE_ID", raising=False)
    if async_jobs is None:
        monkeypatch.delenv("VOICE_ASYNC_JOBS", raising=False)
    else:
        monkeypatch.setenv("VOICE_ASYNC_JOBS", async_jobs)
    # His voice setting: English, so no translation call is made.
    monkeypatch.setattr(
        "app.voice_language.voice_language_for", AsyncMock(return_value="en")
    )
    app = FastAPI()
    register_routes(app)
    monkeypatch.setattr("app.routes._get_user_id", AsyncMock(return_value="owner"))
    return app


def _fish(monkeypatch, handler):
    """Route Fish's client to ``handler``; returns the real client type."""
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )
    return client_type


@pytest.mark.asyncio
async def test_queued_japanese_line_is_spoken_by_fish(monkeypatch):
    app = _app(monkeypatch)
    sent = []

    def fish(request):
        assert str(request.url) == "https://api.fish.audio/v1/tts"
        assert request.headers["model"] == "s2.1-pro-free"
        sent.append(json.loads(request.content))
        return httpx.Response(200, content=WAV, headers={"Content-Type": "audio/wav"})

    client_type = _fish(monkeypatch, fish)
    try:
        async with client_type(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            submitted = await client.post(
                "/api/tts", json={"text": "準備はできています。", "language": "ja"}
            )
            assert submitted.status_code == 202
            assert submitted.headers["Cache-Control"] == "no-store"
            await asyncio.wait_for(app.state.voice_jobs._queue.join(), timeout=1)
            result = await client.get(f'/api/tts/jobs/{submitted.json()["job_id"]}')
            assert result.json()["status"] == "completed"
            assert base64.b64decode(result.json()["audio"]) == WAV
            assert result.headers["Cache-Control"] == "no-store"
        assert [(body["text"], body["language"]) for body in sent] == [
            ("準備はできています。", "ja")
        ]
    finally:
        await app.state.voice_jobs.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["offline", "upstream-auth", "no-credit"])
async def test_failed_job_keeps_session_and_next_request_recovers(monkeypatch, failure):
    app = _app(monkeypatch)
    online = False

    def fish(request):
        if not online:
            if failure == "offline":
                raise httpx.ConnectError("Fish is unreachable", request=request)
            return httpx.Response(401 if failure == "upstream-auth" else 402)
        return httpx.Response(200, content=WAV)

    client_type = _fish(monkeypatch, fish)
    try:
        async with client_type(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post(
                "/api/tts", json={"text": "This text remains available without speech."}
            )
            assert first.status_code == 202
            await asyncio.wait_for(app.state.voice_jobs._queue.join(), timeout=1)
            failed = await client.get(f'/api/tts/jobs/{first.json()["job_id"]}')
            # The browser treats 401 as logout. A voice failure stays job data.
            assert failed.status_code == 200
            assert failed.json() == {
                "job_id": first.json()["job_id"],
                "status": "failed",
                "error": "Voice service unavailable",
            }
            online = True
            second = await client.post("/api/tts", json={"text": "Fish is back."})
            assert second.status_code == 202
            await asyncio.wait_for(app.state.voice_jobs._queue.join(), timeout=1)
            recovered = await client.get(f'/api/tts/jobs/{second.json()["job_id"]}')
            assert recovered.json()["status"] == "completed"
            assert base64.b64decode(recovered.json()["audio"]) == WAV
    finally:
        await app.state.voice_jobs.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "language,text",
    [("en", "x" * 501), ("ja", "あ" * 501), ("fr", "Bonjour.")],
    ids=["long-en", "long-ja", "other-language"],
)
async def test_queued_speech_is_bounded_and_en_or_ja(monkeypatch, language, text):
    app = _app(monkeypatch)

    def no_network(_request):
        pytest.fail("Rejected speech must not contact Fish")

    client_type = _fish(monkeypatch, no_network)
    try:
        async with client_type(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/tts", json={"text": text, "language": language}
            )
            assert response.status_code == 400
    finally:
        await app.state.voice_jobs.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [None, "false", "", "invalid"])
async def test_default_and_disabled_flag_keep_synchronous_response(monkeypatch, flag):
    app = _app(monkeypatch, async_jobs=flag)
    monkeypatch.setattr(
        "app.voice_client.post_tts",
        AsyncMock(return_value=httpx.Response(200, content=b"audio")),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/tts", json={"text": "Hello"})
            assert response.status_code == 200
            assert response.json() == {"audio": "YXVkaW8="}
    finally:
        await app.state.voice_jobs.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["true", " TRUE ", "1", "yes", "on"])
async def test_queued_speech_accepts_500_character_requests(monkeypatch, flag):
    from app.voice_jobs import VoiceJobs

    app = _app(monkeypatch, async_jobs=flag)
    app.state.voice_jobs = VoiceJobs(synthesize=AsyncMock(return_value="YXVkaW8="))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/tts", json={"text": "x" * 500})
            assert response.status_code == 202
    finally:
        await app.state.voice_jobs.close()


@pytest.mark.asyncio
async def test_cancelling_a_running_job_lets_the_next_one_speak(monkeypatch):
    app = _app(monkeypatch)
    started = asyncio.Event()
    synthesizing = []

    async def fish(request):
        text = json.loads(request.content)["text"]
        synthesizing.append(text)
        if text == "first":
            started.set()
            await asyncio.Event().wait()
        return httpx.Response(200, content=WAV)

    client_type = _fish(monkeypatch, fish)
    try:
        async with client_type(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = app.state.voice_jobs.submit("owner", {"text": "first"})
            await asyncio.wait_for(started.wait(), timeout=1)
            second = app.state.voice_jobs.submit("owner", {"text": "second"})
            cancelled = await client.delete(f'/api/tts/jobs/{first["job_id"]}')
            assert cancelled.json()["status"] == "cancelled"
            assert "audio" not in cancelled.json()
            await asyncio.wait_for(app.state.voice_jobs._queue.join(), timeout=1)
            completed = await client.get(f'/api/tts/jobs/{second["job_id"]}')
            assert completed.json()["status"] == "completed"
            assert synthesizing == ["first", "second"]
    finally:
        await app.state.voice_jobs.close()
