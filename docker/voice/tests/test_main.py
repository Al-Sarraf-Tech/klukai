"""Unit coverage for the lazy, pinned companion voice runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from app import main as voice


def _write_voice_lease(
    monkeypatch,
    tmp_path,
    token: str,
    *,
    workload: str = "companion-voice",
    state: str = "active",
    issued_at: float | None = None,
    ttl_seconds: int = 600,
):
    marker = tmp_path / "non-llm-lease.json"
    issued = time.time() if issued_at is None else issued_at
    marker.write_text(
        json.dumps(
            {
                "version": 1,
                "lease_id": "a" * 32,
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "workload": workload,
                "issued_at_epoch_seconds": issued,
                "expires_at_epoch_seconds": issued + ttl_seconds,
                "ttl_seconds": ttl_seconds,
                "state": state,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(voice, "GPU_LEASE_MARKER", marker)
    return marker


def test_load_xtts_passes_snapshot_directory_not_checkpoint_file(monkeypatch, tmp_path):
    """Coqui's TTS() joins model_path with "model.pth" itself; passing the
    checkpoint file as model_path produces a doubled, unopenable path
    (".../model.pth/model.pth") that only surfaces against real model files."""
    model_dir = tmp_path / "xtts-v2"
    model_dir.mkdir()
    for filename in (
        "config.json",
        "dvae.pth",
        "mel_stats.pth",
        "model.pth",
        "speakers_xtts.pth",
        "vocab.json",
    ):
        (model_dir / filename).write_text("x")

    monkeypatch.setattr(voice, "XTTS_MODEL_DIR", model_dir)
    monkeypatch.setattr(voice, "_tts_model", None)
    monkeypatch.setattr(voice, "_tts_ready", False)

    captured = {}

    class FakeTTS:
        def __init__(self, model_path, config_path, **kwargs):
            captured["model_path"] = model_path
            captured["config_path"] = config_path

    fake_tts_api = SimpleNamespace(TTS=FakeTTS)
    monkeypatch.setitem(sys.modules, "TTS", SimpleNamespace(api=fake_tts_api))
    monkeypatch.setitem(sys.modules, "TTS.api", fake_tts_api)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(load=lambda *a, **kw: None))

    result = voice._load_xtts()

    assert captured["model_path"] == str(model_dir)
    assert captured["config_path"] == str(model_dir / "config.json")
    assert result is not None


def test_voice_ttl_cannot_exceed_fifteen_minutes(monkeypatch):
    monkeypatch.setenv("VOICE_MODEL_TTL", "999999")

    assert voice._bounded_ttl_env("VOICE_MODEL_TTL", 600) == 900


def test_voice_ttl_cannot_disable_unloading(monkeypatch):
    monkeypatch.setenv("VOICE_MODEL_TTL", "0")

    assert voice._bounded_ttl_env("VOICE_MODEL_TTL", 600) == 1


def test_server_owned_voice_deadline_margin_is_below_global_ceiling():
    assert voice.MODEL_IDLE_SAFETY_CUTOFF_SECONDS == 895
    assert voice.MODEL_IDLE_SCAN_INTERVAL_SECONDS < 1
    assert (
        voice.MODEL_IDLE_SAFETY_CUTOFF_SECONDS
        + voice.MODEL_IDLE_SCAN_INTERVAL_SECONDS
        <= voice.MAX_MODEL_IDLE_TTL_SECONDS
    )


def test_full_public_ttl_is_clamped_to_server_safety_cutoff(monkeypatch):
    monkeypatch.setenv("VOICE_MODEL_TTL", "900")

    configured = voice._bounded_ttl_env("VOICE_MODEL_TTL", 600)
    effective = min(configured, voice.MODEL_IDLE_SAFETY_CUTOFF_SECONDS)

    assert configured == 900
    assert effective == 895


def test_deadline_scan_unloads_xtts_and_whisper_by_latest_safe_scan(monkeypatch):
    last_use = 100.0
    latest_scan = (
        last_use
        + voice.MODEL_IDLE_SAFETY_CUTOFF_SECONDS
        + voice.MODEL_IDLE_SCAN_INTERVAL_SECONDS
    )
    unloaded = []

    monkeypatch.setattr(
        voice, "VOICE_MODEL_TTL", voice.MODEL_IDLE_SAFETY_CUTOFF_SECONDS
    )
    monkeypatch.setattr(voice, "_tts_model", object())
    monkeypatch.setattr(voice, "_whisper_model", object())
    monkeypatch.setattr(voice, "_last_tts_use", last_use)
    monkeypatch.setattr(voice, "_last_whisper_use", last_use)
    monkeypatch.setattr(voice.time, "monotonic", lambda: latest_scan)
    monkeypatch.setattr(voice, "_unload_xtts", lambda: unloaded.append("xtts"))
    monkeypatch.setattr(
        voice, "_unload_whisper", lambda: unloaded.append("whisper")
    )

    asyncio.run(voice._unload_expired_models(latest_scan))

    assert unloaded == ["xtts", "whisper"]
    assert latest_scan - last_use == 895.5
    assert latest_scan - last_use <= voice.MAX_MODEL_IDLE_TTL_SECONDS


def test_health_does_not_load_models(monkeypatch):
    monkeypatch.setattr(voice, "_tts_model", None)
    monkeypatch.setattr(voice, "_whisper_model", None)
    monkeypatch.setattr(voice, "_tts_ready", False)

    with TestClient(voice.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "companion-voice",
        "tts_engine": voice.TTS_ENGINE,
        "tts_ready": False,
        "whisper_loaded": False,
        "lazy_loading": True,
        "voice_model_ttl": voice.VOICE_MODEL_TTL,
    }


def test_protected_route_requires_configured_bearer(monkeypatch):
    monkeypatch.setattr(voice, "VOICE_API_TOKEN", "")
    with TestClient(voice.app) as client:
        assert client.post("/unload").status_code == 503

    monkeypatch.setattr(voice, "VOICE_API_TOKEN", "rotated-test-token")

    with TestClient(voice.app) as client:
        assert client.post("/unload").status_code == 401
        assert (
            client.post(
                "/unload",
                headers={"Authorization": "Bearer rotated-test-token"},
            ).status_code
            == 200
        )


def test_tts_is_loaded_only_on_request(monkeypatch, tmp_path):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF-reference")
    monkeypatch.setattr(voice, "REFERENCE_WAV", str(reference))

    class FakeTTS:
        def tts_to_file(self, *, text, speaker_wav, language, file_path):
            assert text == "hello"
            assert speaker_wav == str(reference)
            assert language == "en"
            with open(file_path, "wb") as output:
                output.write(b"RIFF-synthesized")

    load_count = 0

    def load_model():
        nonlocal load_count
        load_count += 1
        return FakeTTS()

    monkeypatch.setattr(voice, "_load_xtts", load_model)
    monkeypatch.setattr(voice, "_last_tts_use", 0.0)
    monkeypatch.setattr(voice, "VOICE_API_TOKEN", "test-voice-token")
    _write_voice_lease(monkeypatch, tmp_path, "test-gpu-lease")

    with TestClient(voice.app) as client:
        assert load_count == 0
        response = client.post(
            "/tts",
            json={"text": "hello"},
            headers={
                "Authorization": "Bearer test-voice-token",
                "X-GPU-Lease-Token": "test-gpu-lease",
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content == b"RIFF-synthesized"
    assert load_count == 1
    assert voice._last_tts_use > 0


def test_whisper_prefers_pinned_local_snapshot(monkeypatch, tmp_path):
    snapshot = tmp_path / "whisper-base-en"
    snapshot.mkdir()
    for filename in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
        (snapshot / filename).write_bytes(b"model")
    cache = tmp_path / "cache"
    seen = {}

    class FakeWhisper:
        def __init__(self, source, **kwargs):
            seen["source"] = source
            seen.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(WhisperModel=FakeWhisper),
    )
    monkeypatch.setattr(voice, "WHISPER_MODEL_DIR", snapshot)
    monkeypatch.setattr(voice, "MODEL_DIR", cache)
    monkeypatch.setattr(voice, "_whisper_model", None)

    model = voice._load_whisper()

    assert isinstance(model, FakeWhisper)
    assert seen == {
        "source": str(snapshot),
        "device": "cpu",
        "compute_type": "int8",
        "download_root": str(cache / "whisper"),
    }


def test_explicit_unload_releases_model(monkeypatch):
    moved_to = []

    class FakeTTS:
        def to(self, device):
            moved_to.append(device)

    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(
            is_available=lambda: False,
            empty_cache=lambda: None,
            ipc_collect=lambda: None,
        )
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(voice, "_tts_model", FakeTTS())
    monkeypatch.setattr(voice, "_whisper_model", object())
    monkeypatch.setattr(voice, "_tts_ready", True)

    result = asyncio.run(voice.unload_models())

    assert result == {
        "status": "ok",
        "tts_unloaded": True,
        "whisper_unloaded": True,
        "tts_loaded": False,
        "whisper_loaded": False,
    }
    assert moved_to == ["cpu"]
    assert voice._tts_model is None
    assert voice._tts_ready is False
    assert voice._whisper_model is None


def test_tts_rejects_missing_lease_before_model_load(monkeypatch, tmp_path):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF-reference")
    loads = []
    monkeypatch.setattr(voice, "REFERENCE_WAV", str(reference))
    monkeypatch.setattr(voice, "VOICE_API_TOKEN", "voice-secret")
    monkeypatch.setattr(voice, "GPU_LEASE_MARKER", tmp_path / "missing.json")
    monkeypatch.setattr(voice, "_load_xtts", lambda: loads.append(True))

    with TestClient(voice.app) as client:
        response = client.post(
            "/tts",
            headers={"Authorization": "Bearer voice-secret"},
            json={"text": "hello"},
        )

    assert response.status_code == 403
    assert loads == []


@pytest.mark.parametrize(
    ("workload", "state", "issued_offset", "token", "status"),
    [
        ("comfyui", "active", 0, "valid-token", 403),
        ("companion-voice", "cleaning", 0, "valid-token", 503),
        ("companion-voice", "cleanup_failed", 0, "valid-token", 503),
        ("companion-voice", "active", -601, "valid-token", 503),
        ("companion-voice", "active", 0, "wrong-token", 403),
    ],
)
def test_xtts_lease_validation_fails_closed(
    monkeypatch,
    tmp_path,
    workload,
    state,
    issued_offset,
    token,
    status,
):
    _write_voice_lease(
        monkeypatch,
        tmp_path,
        "valid-token",
        workload=workload,
        state=state,
        issued_at=time.time() + issued_offset,
    )

    with pytest.raises(voice.HTTPException) as caught:
        voice._require_xtts_gpu_lease(token)

    assert caught.value.status_code == status
    assert "valid-token" not in str(caught.value.detail)
    assert "wrong-token" not in str(caught.value.detail)


def test_xtts_lease_validation_accepts_matching_active_capability(
    monkeypatch, tmp_path
):
    _write_voice_lease(monkeypatch, tmp_path, "valid-token")

    voice._require_xtts_gpu_lease("valid-token")


def test_failed_xtts_cpu_handoff_keeps_model_for_fail_closed_retry(monkeypatch):
    class StuckTTS:
        def to(self, _device):
            raise RuntimeError("driver busy")

    model = StuckTTS()
    monkeypatch.setattr(voice, "_tts_model", model)
    monkeypatch.setattr(voice, "_tts_ready", True)

    with pytest.raises(voice.HTTPException) as caught:
        asyncio.run(voice.unload_models())

    assert caught.value.status_code == 503
    assert voice._tts_model is model
    assert voice._tts_ready is True


def test_cancelled_tts_keeps_access_lock_until_native_worker_finishes(
    monkeypatch, tmp_path
):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFF-reference")
    started = threading.Event()
    finish = threading.Event()

    class BlockingTTS:
        def tts_to_file(self, *, file_path, **_kwargs):
            started.set()
            assert finish.wait(timeout=2)
            with open(file_path, "wb") as output:
                output.write(b"RIFF-synthesized")

    monkeypatch.setattr(voice, "REFERENCE_WAV", str(reference))
    monkeypatch.setattr(voice, "_load_xtts", lambda: BlockingTTS())
    _write_voice_lease(monkeypatch, tmp_path, "valid-token")

    async def scenario():
        task = asyncio.create_task(
            voice.text_to_speech(voice.TTSRequest(text="hello"), "valid-token")
        )
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0.05)
        assert voice._tts_access_lock.locked()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not voice._tts_access_lock.locked()

    asyncio.run(scenario())
