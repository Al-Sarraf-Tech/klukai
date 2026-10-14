"""Exercise the XPU shell without loading Torch or an inference model."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
import wave

import httpx
import pytest

SOURCE = Path(__file__).parents[1]
sys.path.insert(0, str(SOURCE))


def runtime():
    assert (SOURCE / "voice_xpu" / "server.py").is_file(), "local voice runtime is missing"
    from voice_xpu import server
    return server


@pytest.fixture
def fake_worker(tmp_path):
    # Real process and pipes exercise worker retention, EOF, and kill behavior.
    path = tmp_path / "worker.py"
    path.write_text('''import base64, io, json, os, sys, time, wave
for line in sys.stdin:
    request = json.loads(line)
    if request["text"] == "slow":
        time.sleep(30)
    if request["text"] == "crash":
        sys.exit(7)
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
        wav.writeframes(b"\\x01\\x00" * 64)
    print(json.dumps({"audio": base64.b64encode(out.getvalue()).decode(), "sample_rate": 24000}), flush=True)
''')
    return (sys.executable, "-u", str(path))


def controller(fake_worker, **kwargs):
    service = runtime()
    settings = service.Settings(token="test-secret", poll_seconds=0.01, **kwargs)
    return service.WorkerController(settings, command=fake_worker, guard=lambda: None)


async def connected():
    return False


def test_model_free_health_and_authorization_do_not_spawn_worker(fake_worker):
    async def scenario():
        ctl = controller(fake_worker)
        app = runtime().create_app(ctl.settings, ctl)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://voice") as client:
            response = await client.get("/health")
            assert response.status_code == 200
            assert response.json()["tts_ready"] is False
            for route, payload in (("/tts", {"text": "hello"}), ("/unload", {})):
                denied = await client.post(route, json=payload)
                assert denied.status_code == 401
            assert ctl.pid is None
    asyncio.run(scenario())


def test_missing_token_fails_closed(fake_worker):
    async def scenario():
        ctl = controller(fake_worker)
        settings = runtime().Settings(token="")
        app = runtime().create_app(settings, ctl)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://voice") as client:
            denied = await client.post("/tts", json={"text": "hello"}, headers={"Authorization": "Bearer "})
            assert denied.status_code == 503
            assert ctl.pid is None
    asyncio.run(scenario())


def test_success_retains_worker_and_unload_reaps_it(fake_worker):
    async def scenario():
        ctl = controller(fake_worker)
        result = await ctl.synthesize("hello", "en", connected)
        original_pid = ctl.pid
        assert original_pid
        with wave.open(io.BytesIO(base64.b64decode(result["audio"]))) as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (24000, 1, 2)
            assert wav.getnframes() == 64
        await ctl.synthesize("again", "en", connected)
        assert ctl.pid == original_pid
        await ctl.unload()
        assert ctl.pid is None
        with pytest.raises(ProcessLookupError):
            os.kill(original_pid, 0)
    asyncio.run(scenario())


def test_concurrent_requests_are_rejected_instead_of_queued(fake_worker):
    async def scenario():
        ctl = controller(fake_worker)
        task = asyncio.create_task(ctl.synthesize("slow", "en", connected))
        try:
            while ctl.pid is None:
                await asyncio.sleep(0.001)
            with pytest.raises(runtime().VoiceError) as raised:
                await ctl.synthesize("hello", "en", connected)
            assert raised.value.status == 429
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert ctl.pid is None
        assert not ctl.busy
    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["disconnect", "deadline", "thermal", "cancel"])
def test_failure_kills_worker_and_allows_clean_retry(fake_worker, reason):
    async def scenario():
        ctl = controller(fake_worker, request_seconds=0.1 if reason == "deadline" else 10)
        disconnected = False
        hot = False
        async def connection_state():
            return disconnected
        def guard():
            if hot:
                raise runtime().VoiceError(503, "temperature limit")
        ctl.guard = guard
        task = asyncio.create_task(ctl.synthesize("slow", "en", connection_state))
        while ctl.pid is None:
            await asyncio.sleep(0.001)
        original_pid = ctl.pid
        if reason == "disconnect":
            disconnected = True
        elif reason == "thermal":
            hot = True
        elif reason == "cancel":
            task.cancel()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(result, (runtime().VoiceError, asyncio.CancelledError))
        assert ctl.pid is None
        with pytest.raises(ProcessLookupError):
            os.kill(original_pid, 0)
        hot = False
        await ctl.synthesize("hello", "en", connected)
        await ctl.unload()
    asyncio.run(scenario())


def test_idle_worker_expires_but_active_request_is_not_expired(fake_worker):
    async def scenario():
        ctl = controller(fake_worker, idle_seconds=0.02)
        await ctl.synthesize("hello", "en", connected)
        await asyncio.sleep(0.03)
        await ctl.expire_idle()
        assert ctl.pid is None
        task = asyncio.create_task(ctl.synthesize("slow", "en", connected))
        while ctl.pid is None:
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.03)
        await ctl.expire_idle()
        assert ctl.pid is not None
        await ctl.unload()
        await asyncio.gather(task, return_exceptions=True)
        assert ctl.pid is None
    asyncio.run(scenario())


def test_worker_crash_fails_without_retaining_dead_process(fake_worker):
    async def scenario():
        ctl = controller(fake_worker)
        with pytest.raises(runtime().VoiceError):
            await ctl.synthesize("crash", "en", connected)
        assert ctl.pid is None
    asyncio.run(scenario())


def test_thermal_guard_requires_both_sensor_classes_and_checks_boundary(tmp_path):
    service = runtime()
    cpu = tmp_path / "hwmon0"
    cpu.mkdir()
    (cpu / "name").write_text("coretemp")
    (cpu / "temp1_input").write_text("44000")
    with pytest.raises(service.VoiceError):
        service.check_temperatures(tmp_path)
    gpu = tmp_path / "hwmon1"
    gpu.mkdir()
    (gpu / "name").write_text("i915")
    (gpu / "temp1_input").write_text("53000")
    service.check_temperatures(tmp_path)
    (gpu / "temp1_input").write_text("68000")
    with pytest.raises(service.VoiceError):
        service.check_temperatures(tmp_path)


def test_release_verification_rejects_modified_checkpoint_and_reference(tmp_path):
    runtime()
    from voice_xpu.artifacts import verify_artifacts
    model = tmp_path / "model"
    model.mkdir()
    (model / "model.pth").write_bytes(b"pinned-model")
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"pinned-reference")
    lock = {"model": {"files": [{"path": "model.pth", "size_bytes": 12,
        "sha256": hashlib.sha256(b"pinned-model").hexdigest()}]},
        "reference": {"size_bytes": 16, "sha256": hashlib.sha256(b"pinned-reference").hexdigest()}}
    verify_artifacts(lock, model, reference)
    (model / "model.pth").write_bytes(b"bad-model")
    with pytest.raises(ValueError):
        verify_artifacts(lock, model, reference)
    (model / "model.pth").write_bytes(b"pinned-model")
    reference.write_bytes(b"bad-reference")
    with pytest.raises(ValueError):
        verify_artifacts(lock, model, reference)


def test_http_input_boundaries_and_success(fake_worker):
    async def scenario():
        ctl = controller(fake_worker)
        app = runtime().create_app(ctl.settings, ctl)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://voice",
                headers={"Authorization": "Bearer test-secret"}) as client:
            for payload in ({"text": ""}, {"text": "   "}, {"text": "x" * 501}, {"text": "hi", "language": "xx"}):
                assert (await client.post("/tts", json=payload)).status_code == 422
            assert ctl.pid is None
            response = await client.post("/tts", json={"text": "hello", "language": "en"})
            assert response.status_code == 200
            assert response.json()["sample_rate"] == 24000
            assert (await client.post("/unload")).status_code == 200
            assert ctl.pid is None
    asyncio.run(scenario())


def test_worker_encodes_real_24khz_pcm_and_rejects_bad_samples():
    runtime()
    assert (SOURCE / "voice_xpu" / "worker.py").is_file(), "model worker is missing"
    from voice_xpu.worker import encode_wav
    audio = encode_wav([0.0, 0.25, -0.25], 24000)
    with wave.open(io.BytesIO(base64.b64decode(audio)), "rb") as wav:
        assert wav.getframerate() == 24000
        assert wav.getnframes() == 3
        assert wav.readframes(3) == b"\x00\x00\xff\x1f\x01\xe0"
    for samples, rate in (([], 24000), ([float("nan")], 24000), ([0.2], 22050), ([0.0] * 16, 24000)):
        with pytest.raises(ValueError):
            encode_wav(samples, rate)


def test_float16_zero_position_adapter_keeps_zero_values_and_model_dtype():
    runtime()
    assert (SOURCE / "voice_xpu" / "worker.py").is_file(), "model worker is missing"
    from voice_xpu.worker import install_position_adapter
    from types import SimpleNamespace
    model = SimpleNamespace(gpt=SimpleNamespace(model_dim=4, gpt=SimpleNamespace(
        parameters=lambda: iter([SimpleNamespace(dtype="float16")]))))
    class Torch:
        @staticmethod
        def zeros(shape, *, device, dtype):
            return {"shape": shape, "device": device, "dtype": dtype, "values": [0] * (shape[0] * shape[1] * shape[2])}
    install_position_adapter(model, Torch)
    result = model.gpt.gpt.wpe(SimpleNamespace(shape=(1, 2), device="xpu:0"))
    assert result == {"shape": (1, 2, 4), "device": "xpu:0", "dtype": "float16", "values": [0] * 8}


def test_japanese_is_rejected_before_loading_the_unprovisioned_tokenizer(fake_worker):
    async def scenario():
        ctl = controller(fake_worker)
        app = runtime().create_app(ctl.settings, ctl)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://voice",
                headers={"Authorization": "Bearer test-secret"}) as client:
            response = await client.post("/tts", json={"text": "こんにちは", "language": "ja"})
            assert response.status_code == 422
            assert ctl.pid is None
    asyncio.run(scenario())


def test_worker_environment_excludes_injection_variables_and_voice_secret(monkeypatch):
    service = runtime()
    monkeypatch.setenv("LD_PRELOAD", "/untrusted/lib.so")
    monkeypatch.setenv("LD_AUDIT", "/untrusted/audit.so")
    monkeypatch.setenv("PYTHONPATH", "/untrusted")
    monkeypatch.setenv("VOICE_API_TOKEN", "do-not-pass-this-to-model")
    environment = service.clean_worker_environment()
    assert not any(key.startswith("LD_") for key in environment)
    assert "PYTHONPATH" not in environment
    assert "VOICE_API_TOKEN" not in environment
    assert environment["HF_HUB_OFFLINE"] == "1"


def test_unload_during_active_request_reaps_before_admitting_retry(fake_worker):
    async def scenario():
        ctl = controller(fake_worker)
        task = asyncio.create_task(ctl.synthesize("slow", "en", connected))
        while ctl.pid is None:
            await asyncio.sleep(0.001)
        await ctl.unload()
        await asyncio.gather(task, return_exceptions=True)
        assert ctl.pid is None and not ctl.busy
        assert (await ctl.synthesize("hello", "en", connected))["sample_rate"] == 24000
        await ctl.close()
        with pytest.raises(runtime().VoiceError):
            await ctl.synthesize("hello", "en", connected)
    asyncio.run(scenario())


def test_non_ascii_bearer_is_unauthorized_not_a_server_error(fake_worker):
    async def scenario():
        ctl = controller(fake_worker)
        app = runtime().create_app(ctl.settings, ctl)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://voice") as client:
            response = await client.post("/tts", json={"text": "hello"},
                                         headers={b"Authorization": b"Bearer \xff"})
            assert response.status_code == 401
            assert ctl.pid is None
    asyncio.run(scenario())


def test_asgi_client_disconnect_reaps_worker(fake_worker):
    async def scenario():
        ctl = controller(fake_worker)
        app = runtime().create_app(ctl.settings, ctl)
        body = json.dumps({"text": "slow", "language": "en"}).encode()
        incoming = asyncio.Queue()
        incoming.put_nowait({"type": "http.request", "body": body, "more_body": False})
        sent = []
        async def send(message):
            sent.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/tts", "raw_path": b"/tts",
                 "query_string": b"", "root_path": "", "server": ("voice", 80),
                 "client": ("127.0.0.1", 1234),
                 "headers": [(b"content-type", b"application/json"),
                             (b"authorization", b"Bearer test-secret")]}
        task = asyncio.create_task(app(scope, incoming.get, send))
        while ctl.pid is None:
            await asyncio.sleep(0.001)
        original_pid = ctl.pid
        incoming.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(task, 3)
        assert ctl.pid is None and not ctl.busy
        with pytest.raises(ProcessLookupError):
            os.kill(original_pid, 0)
    asyncio.run(scenario())
