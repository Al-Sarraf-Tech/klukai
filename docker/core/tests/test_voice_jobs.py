"""Bounded voice work stays private, ordered, and cancellable."""

import asyncio
import base64

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
    monkeypatch.setenv("VOICE_BACKEND", "amarillo-xpu")

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
async def test_local_voice_bypasses_dominus_lease_and_decodes_json(monkeypatch):
    from app.voice_client import post_leased_tts, stt_url

    monkeypatch.setenv("VOICE_BACKEND", "amarillo-xpu")
    monkeypatch.setenv("VOICE_URL", "http://local:8301")
    monkeypatch.setenv("VOICE_API_TOKEN", "test-only")
    monkeypatch.delenv("VOICE_STT_URL", raising=False)

    def forbidden(*_args, **_kwargs):
        pytest.fail("local voice contacted the Dominus lease gate")

    monkeypatch.setattr("app.voice_client.gpu_lease", forbidden)
    monkeypatch.setattr("app.llm_router.get_lm_gate", forbidden)
    raw = b"RIFF\x00WAVE"

    def handle(request):
        assert request.headers["Authorization"] == "Bearer test-only"
        assert "X-GPU-Lease-Token" not in request.headers
        return httpx.Response(200, json={"audio": base64.b64encode(raw).decode()})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await post_leased_tts(client, {"text": "test"}, timeout=600)
    assert result.content == raw
    assert stt_url() == "http://100.107.121.5:8301"


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_status", [200, 503])
async def test_synthesize_handles_normalized_local_response_status(monkeypatch, upstream_status):
    from app import voice_jobs

    monkeypatch.setenv("VOICE_BACKEND", "amarillo-xpu")
    monkeypatch.setenv("VOICE_URL", "http://local:8301")
    monkeypatch.setenv("VOICE_API_TOKEN", "test-only")
    client_type = httpx.AsyncClient

    def handle(request):
        return httpx.Response(upstream_status, json={"audio": "YXVkaW8="})

    monkeypatch.setattr(voice_jobs.httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(handle), **kwargs
    ))
    if upstream_status == 200:
        assert await voice_jobs._synthesize({"text": "Hello.", "language": "en"}) == "YXVkaW8="
    else:
        with pytest.raises(httpx.HTTPStatusError) as failed:
            await voice_jobs._synthesize({"text": "Hello.", "language": "en"})
        assert failed.value.response.status_code == 503


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
        return 100, 0

    monkeypatch.setattr("app.rate_limit.check_and_consume", consume)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/tts")).status_code == 429
        assert (await client.get("/api/tts/jobs/test")).status_code == 200
        assert (await client.delete("/api/tts/jobs/test")).status_code == 200


@pytest.mark.asyncio
async def test_cancel_waits_for_native_unload_before_starting_next_synthesis(monkeypatch):
    from app import voice_client

    first_started = asyncio.Event()
    unloading = asyncio.Event()
    reaped = asyncio.Event()
    second_started = asyncio.Event()
    monkeypatch.setenv("VOICE_BACKEND", "amarillo-xpu")
    monkeypatch.setenv("VOICE_API_TOKEN", "test-only")
    monkeypatch.setattr(voice_client, "_local_gate", asyncio.Lock())

    async def unload():
        unloading.set()
        await reaped.wait()

    monkeypatch.setattr(voice_client, "_unload_local_voice", unload, raising=False)

    async def handle(request):
        if b'first' in request.content:
            first_started.set()
            await asyncio.Event().wait()
        second_started.set()
        return httpx.Response(200, json={"audio": "YXVkaW8="})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        first = asyncio.create_task(voice_client.post_leased_tts(client, {"text": "first"}))
        await first_started.wait()
        second = asyncio.create_task(voice_client.post_leased_tts(client, {"text": "second"}))
        first.cancel()
        try:
            await asyncio.wait_for(unloading.wait(), timeout=0.1)
            assert not second_started.is_set()
            reaped.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert (await second).content == b"audio"
        finally:
            reaped.set()
            first.cancel()
            second.cancel()
            await asyncio.gather(first, second, return_exceptions=True)
