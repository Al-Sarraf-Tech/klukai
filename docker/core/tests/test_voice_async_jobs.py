"""Dominus can opt into queued speech without changing its GPU lease boundary."""

import asyncio
import base64
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI


def _app(monkeypatch, *, backend="dominus", async_jobs="true"):
    from app.routes import register_routes

    monkeypatch.setenv("VOICE_BACKEND", backend)
    if async_jobs is None:
        monkeypatch.delenv("VOICE_ASYNC_JOBS", raising=False)
    else:
        monkeypatch.setenv("VOICE_ASYNC_JOBS", async_jobs)
    app = FastAPI()
    register_routes(app)
    monkeypatch.setattr("app.routes._get_user_id", AsyncMock(return_value="owner"))
    return app


def _upstream(monkeypatch, handler):
    """Replace only network I/O, retaining real jobs, gates, and lease handling."""
    from app import gpu_lease, voice_jobs

    client_type = httpx.AsyncClient
    monkeypatch.setenv("VOICE_URL", "http://voice.test:8301")
    monkeypatch.setenv("LM_STUDIO_URL", "http://gateway.test:1234")
    monkeypatch.setenv("VOICE_API_TOKEN", "voice-only")
    monkeypatch.setenv("LM_STUDIO_TOKEN", "gateway-only")
    monkeypatch.setattr(gpu_lease, "_publish_lease_event", AsyncMock())
    gate = asyncio.Lock()
    monkeypatch.setattr("app.llm_router.get_lm_gate", lambda: gate)
    monkeypatch.setattr(voice_jobs.httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(handler), **kwargs
    ))
    return client_type, gate


@pytest.mark.parametrize("remote_default", [30.0, 60.0])
@pytest.mark.parametrize("backend,flag,extended", [
    ("dominus", "true", True),
    ("dominus", "false", False),
    ("dominus", None, False),
    ("amarillo-xpu", "false", True),
])
def test_voice_timeout_uses_opt_in_for_microphone_and_archive(
    monkeypatch, remote_default, backend, flag, extended,
):
    from app.voice_client import tts_timeout

    monkeypatch.setenv("VOICE_BACKEND", backend)
    if flag is None:
        monkeypatch.delenv("VOICE_ASYNC_JOBS", raising=False)
    else:
        monkeypatch.setenv("VOICE_ASYNC_JOBS", flag)
    assert tts_timeout(remote_default) == (600.0 if extended else remote_default)


@pytest.mark.asyncio
async def test_dominus_async_job_preserves_japanese_binary_wav_and_gpu_lease(monkeypatch):
    app = _app(monkeypatch)
    raw = b"RIFF\x04\x00\x00\x00WAVE"
    events = []

    def upstream(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/acquire"):
            events.append("acquired")
            assert body["workload"] == "companion-voice"
            assert body["ttl_seconds"] == 600
            return httpx.Response(200, json={"status": "acquired", "ttl_seconds": 600, "lease_token": "opaque-capability"})
        if request.url.path == "/tts":
            events.append("synthesis")
            assert gate.locked()
            assert request.headers["Authorization"] == "Bearer voice-only"
            assert request.headers["X-GPU-Lease-Token"] == "opaque-capability"
            assert request.extensions["timeout"]["read"] == 600
            assert body == {"text": "準備はできています。", "language": "ja"}
            return httpx.Response(200, content=raw, headers={"Content-Type": "audio/wav"})
        assert request.url.path.endswith("/release")
        events.append("released")
        assert body == {"lease_token": "opaque-capability"}
        return httpx.Response(204)

    client_type, gate = _upstream(monkeypatch, upstream)
    try:
        async with client_type(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            submitted = await client.post("/api/tts", json={"text": "準備はできています。", "language": "ja"})
            assert submitted.status_code == 202
            assert submitted.headers["Cache-Control"] == "no-store"
            await asyncio.wait_for(app.state.voice_jobs._queue.join(), timeout=1)
            result = await client.get(f'/api/tts/jobs/{submitted.json()["job_id"]}')
            assert result.json()["status"] == "completed"
            assert base64.b64decode(result.json()["audio"]) == raw
            assert result.headers["Cache-Control"] == "no-store"
        assert events == ["acquired", "synthesis", "released"]
        assert not gate.locked()
    finally:
        await app.state.voice_jobs.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,language,text", [
    ("dominus", "en", "x" * 501),
    ("dominus", "ja", "あ" * 501),
    ("amarillo-xpu", "en", "x" * 501),
    ("amarillo-xpu", "ja", "こんにちは"),
], ids=["dominus-long-en", "dominus-long-ja", "arc-long-en", "arc-ja"])
async def test_queued_speech_is_bounded_and_arc_stays_english_only(monkeypatch, backend, language, text):
    app = _app(monkeypatch, backend=backend)

    def no_network(_request):
        pytest.fail("Rejected speech must not contact a GPU service")

    client_type, _gate = _upstream(monkeypatch, no_network)
    try:
        async with client_type(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/tts", json={"text": text, "language": language})
            assert response.status_code == 400
    finally:
        await app.state.voice_jobs.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [None, "false", "", "invalid"])
async def test_dominus_default_and_disabled_flag_keep_synchronous_response(monkeypatch, flag):
    app = _app(monkeypatch, async_jobs=flag)
    monkeypatch.setattr("app.voice_client.post_leased_tts", AsyncMock(return_value=httpx.Response(200, content=b"audio")))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/tts", json={"text": "Hello"})
            assert response.status_code == 200
            assert response.json() == {"audio": "YXVkaW8="}
    finally:
        await app.state.voice_jobs.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,flag", [
    ("amarillo-xpu", "false"),
    ("dominus", "true"),
    ("dominus", " TRUE "),
    ("dominus", "1"),
    ("dominus", "yes"),
    ("dominus", "on"),
])
async def test_async_backends_accept_500_character_requests(monkeypatch, backend, flag):
    from app.voice_jobs import VoiceJobs

    app = _app(monkeypatch, backend=backend, async_jobs=flag)
    app.state.voice_jobs = VoiceJobs(synthesize=AsyncMock(return_value="YXVkaW8="))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/tts", json={"text": "x" * 500})
            assert response.status_code == 202
    finally:
        await app.state.voice_jobs.close()


@pytest.mark.asyncio
async def test_dominus_job_cancellation_waits_for_lease_release_before_next_job(monkeypatch):
    app = _app(monkeypatch)
    started, cleaning, allow_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    synthesizing = []

    async def upstream(request):
        if request.url.path.endswith("/acquire"):
            return httpx.Response(200, json={"status": "acquired", "ttl_seconds": 600, "lease_token": "opaque-capability"})
        if request.url.path == "/tts":
            text = json.loads(request.content)["text"]
            synthesizing.append(text)
            if text == "first":
                started.set()
                await asyncio.Event().wait()
            return httpx.Response(200, content=b"wav")
        assert request.url.path.endswith("/release")
        cleaning.set()
        await allow_release.wait()
        return httpx.Response(204)

    client_type, gate = _upstream(monkeypatch, upstream)
    cancellation = None
    try:
        async with client_type(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = app.state.voice_jobs.submit("owner", {"text": "first"})
            await asyncio.wait_for(started.wait(), timeout=1)
            second = app.state.voice_jobs.submit("owner", {"text": "second"})
            cancellation = asyncio.create_task(client.delete(f'/api/tts/jobs/{first["job_id"]}'))
            await asyncio.wait_for(cleaning.wait(), timeout=1)
            assert not cancellation.done()
            assert gate.locked()
            assert synthesizing == ["first"]
            allow_release.set()
            cancelled = await asyncio.wait_for(cancellation, timeout=1)
            assert cancelled.json()["status"] == "cancelled"
            assert "audio" not in cancelled.json()
            await asyncio.wait_for(app.state.voice_jobs._queue.join(), timeout=1)
            completed = await client.get(f'/api/tts/jobs/{second["job_id"]}')
            assert completed.json()["status"] == "completed"
            assert synthesizing == ["first", "second"]
            assert not gate.locked()
    finally:
        allow_release.set()
        if cancellation is not None:
            await asyncio.gather(cancellation, return_exceptions=True)
        await app.state.voice_jobs.close()
