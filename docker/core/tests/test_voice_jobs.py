"""Bounded voice work stays private, ordered, and cancellable."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_queue_serializes_and_cancels_running_and_queued_work():
    from app.voice_jobs import VoiceJobs

    started, cancelled = [], []
    first_started = asyncio.Event()

    async def synth(payload):
        started.append(payload["text"])
        first_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(payload["text"])

    jobs = VoiceJobs(synthesize=synth)
    try:
        first = jobs.submit("owner", {"text": "first"})
        second = jobs.submit("owner", {"text": "second"})
        await first_started.wait()
        with pytest.raises(KeyError):
            jobs.get("other", first["job_id"])
        assert jobs.get("owner", second["job_id"])["status"] == "queued"
        assert await jobs.cancel("owner", second["job_id"]) == {"job_id": second["job_id"], "status": "cancelled"}
        await jobs.cancel("owner", first["job_id"])
        assert cancelled == ["first"]
        assert started == ["first"]
        assert jobs.get("owner", first["job_id"])["status"] == "cancelled"
    finally:
        await jobs.close()


@pytest.mark.asyncio
async def test_cancelled_pending_jobs_immediately_release_queue_capacity():
    from app.voice_jobs import VoiceJobs

    started = asyncio.Event()

    async def synth(_payload):
        started.set()
        await asyncio.Event().wait()

    jobs = VoiceJobs(synthesize=synth, max_outstanding=3)
    try:
        jobs.submit("owner", {"text": "long active request"})
        await started.wait()
        survivor = jobs.submit("other", {"text": "keep this queued"})
        for _ in range(12):
            item = jobs.submit("owner", {"text": "cancel this queued request"})
            await jobs.cancel("owner", item["job_id"])
        assert jobs.get("other", survivor["job_id"])["status"] == "queued"
        accepted = jobs.submit("owner", {"text": "still accepts new speech"})
        assert accepted["status"] == "queued"
    finally:
        await jobs.close()


@pytest.mark.asyncio
async def test_queue_limits_deadline_and_result_size():
    from app.voice_jobs import VoiceJobs, VoiceQueueFull

    async def synth(_payload):
        await asyncio.Event().wait()

    jobs = VoiceJobs(synthesize=synth, deadline=0.03, max_outstanding=3)
    try:
        first = jobs.submit("owner", {"text": "first"})
        jobs.submit("owner", {"text": "second"})
        with pytest.raises(VoiceQueueFull):
            jobs.submit("owner", {"text": "third"})
        jobs.submit("other", {"text": "third"})
        with pytest.raises(VoiceQueueFull):
            jobs.submit("another", {"text": "fourth"})
        await asyncio.sleep(0.06)
        assert jobs.get("owner", first["job_id"])["status"] == "failed"
    finally:
        await jobs.close()

    async def oversized(_payload):
        return "a" * 11

    jobs = VoiceJobs(synthesize=oversized, max_result_bytes=10)
    try:
        item = jobs.submit("owner", {"text": "test"})
        await asyncio.sleep(0.01)
        result = jobs.get("owner", item["job_id"])
        assert result["status"] == "failed"
        assert "audio" not in result
    finally:
        await jobs.close()


@pytest.mark.asyncio
async def test_completed_result_expires_and_shutdown_cancels_upstream():
    from app.voice_jobs import VoiceJobs

    async def synth(_payload):
        return "YXVkaW8="

    jobs = VoiceJobs(synthesize=synth, terminal_ttl=0.02)
    item = jobs.submit("owner", {"text": "hello"})
    await asyncio.sleep(0.01)
    assert jobs.get("owner", item["job_id"])["audio"] == "YXVkaW8="
    await asyncio.sleep(0.03)
    with pytest.raises(KeyError):
        jobs.get("owner", item["job_id"])
    await jobs.close()

    started, cancelled = asyncio.Event(), asyncio.Event()

    async def waiting(_payload):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    jobs = VoiceJobs(synthesize=waiting)
    jobs.submit("owner", {"text": "hello"})
    await started.wait()
    await jobs.close()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_job_routes_require_auth_and_hide_foreign_jobs(monkeypatch):
    from app.routes import register_routes
    from app.voice_jobs import VoiceJobs

    async def synth(_payload):
        return "YXVkaW8="

    app = FastAPI()
    register_routes(app)
    jobs = VoiceJobs(synthesize=synth)
    app.state.voice_jobs = jobs
    monkeypatch.setenv("KLUKAI_VOICE_ENABLED", "true")
    monkeypatch.setenv("VOICE_ASYNC_JOBS", "true")
    monkeypatch.setattr(
        "app.voice_language.voice_language_for", AsyncMock(return_value="en")
    )

    async def user(request):
        return request.headers.get("x-user")

    monkeypatch.setattr("app.routes._get_user_id", user)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post("/api/tts", json={"text": "Hello"})).status_code == 401
            response = await client.post("/api/tts", json={"text": "Hello"}, headers={"x-user": "owner"})
            assert response.status_code == 202
            path = f'/api/tts/jobs/{response.json()["job_id"]}'
            for method in (client.get, client.delete):
                assert (await method(path)).status_code == 401
                unknown = await method("/api/tts/jobs/missing", headers={"x-user": "other"})
                foreign = await method(path, headers={"x-user": "other"})
                assert foreign.status_code == unknown.status_code == 404
                assert foreign.json() == unknown.json()
            await asyncio.sleep(0.01)
            done = await client.get(path, headers={"x-user": "owner"})
            assert done.json()["audio"] == "YXVkaW8="
            assert (await client.delete(path, headers={"x-user": "owner"})).status_code == 200
    finally:
        await jobs.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_status", [200, 502])
async def test_synthesize_encodes_fish_audio_or_raises(monkeypatch, upstream_status):
    from app import voice_jobs

    response = httpx.Response(
        upstream_status,
        content=b"audio",
        request=httpx.Request("POST", "https://api.fish.audio/v1/tts"),
    )
    post_tts = AsyncMock(return_value=response)
    monkeypatch.setattr(voice_jobs, "post_tts", post_tts)
    payload = {"text": "Hello.", "language": "en"}
    if upstream_status == 200:
        assert await voice_jobs._synthesize(payload) == "YXVkaW8="
    else:
        with pytest.raises(httpx.HTTPStatusError) as failed:
            await voice_jobs._synthesize(payload)
        assert failed.value.response.status_code == 502
    post_tts.assert_awaited_once_with(payload)


@pytest.mark.asyncio
async def test_poll_and_cancel_do_not_spend_synthesis_rate_limit(monkeypatch):
    from app.main import _RateLimitMiddleware
    from app.rate_limit import RateLimitExceeded

    app = FastAPI()
    app.add_middleware(_RateLimitMiddleware)

    @app.api_route("/api/tts/jobs/{job_id}", methods=["GET", "DELETE"])
    async def job(job_id: str):
        return {"job_id": job_id, "status": "queued"}

    @app.post("/api/tts")
    async def submit():
        return {"job_id": "test"}

    async def consume(_identity, bucket):
        if bucket == "tts":
            raise RateLimitExceeded(bucket, 30)
        return 100, 0, None

    monkeypatch.setattr("app.rate_limit.consume", consume)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/tts")).status_code == 429
        assert (await client.get("/api/tts/jobs/test")).status_code == 200
        assert (await client.delete("/api/tts/jobs/test")).status_code == 200
