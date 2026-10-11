"""Unit coverage for the lazy, pinned companion voice runtime."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
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


class _FakeWhisper:
    """faster-whisper shape: transcribe() returns a LAZY generator + info.

    The generator records whether it was consumed on a thread with a running
    event loop; the real decode happens during consumption, so consuming it
    on the loop is the bug under test.
    """

    def __init__(self, texts=("hello", "commander"), raise_on_consume=None):
        self.texts = texts
        self.raise_on_consume = raise_on_consume
        self.paths: list[str] = []
        self.consumed_on_loop: bool | None = None

    def _segments(self):
        try:
            asyncio.get_running_loop()
            self.consumed_on_loop = True
        except RuntimeError:
            self.consumed_on_loop = False
        if self.raise_on_consume is not None:
            raise self.raise_on_consume
        for text in self.texts:
            yield SimpleNamespace(text=text)

    def transcribe(self, path, beam_size):
        assert beam_size == 5
        self.paths.append(path)
        assert Path(path).is_file()  # the body must already be on disk
        return self._segments(), SimpleNamespace(language="en", duration=1.5)


def _stt_ready(monkeypatch, model):
    monkeypatch.setattr(voice, "_load_whisper", lambda: model)
    monkeypatch.setattr(voice, "_last_whisper_use", 0.0)
    monkeypatch.setattr(voice, "VOICE_API_TOKEN", "voice-secret")
    return {"Authorization": "Bearer voice-secret"}


def test_transcribe_sync_joins_segments_off_the_loop():
    """The join (= the decode) must happen in the worker, not after to_thread
    hands back a lazy generator."""
    model = _FakeWhisper()

    async def scenario():
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
            tmp.write(b"opus")
        try:
            return await asyncio.to_thread(voice._transcribe_sync, model, tmp.name)
        finally:
            voice._unlink_quietly(tmp.name)

    text, info = asyncio.run(scenario())

    assert text == "hello commander"
    assert info.language == "en"
    assert model.consumed_on_loop is False


def test_stt_decodes_in_the_worker_thread_and_cleans_its_temp_file(monkeypatch):
    """Regression: only model.transcribe() ran in to_thread; the generator
    was joined on the event loop, freezing /health and /tts for the whole
    clip."""
    model = _FakeWhisper()
    headers = _stt_ready(monkeypatch, model)

    with TestClient(voice.app) as client:
        response = client.post(
            "/stt",
            json={"audio": base64.b64encode(b"opus-bytes").decode()},
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json() == {"text": "hello commander", "language": "en", "duration": 1.5}
    assert model.consumed_on_loop is False
    assert len(model.paths) == 1
    assert not Path(model.paths[0]).exists()
    assert voice._last_whisper_use > 0


def test_stt_rejects_oversized_audio_before_decoding(monkeypatch):
    model = _FakeWhisper()
    headers = _stt_ready(monkeypatch, model)

    with TestClient(voice.app) as client:
        response = client.post(
            "/stt", json={"audio": "A" * 8_000_001}, headers=headers
        )

    assert response.status_code == 422
    assert model.paths == []


def test_stt_decode_failure_reports_error_and_removes_temp_file(monkeypatch):
    model = _FakeWhisper(raise_on_consume=RuntimeError("ctranslate2 exploded"))
    headers = _stt_ready(monkeypatch, model)

    with TestClient(voice.app) as client:
        response = client.post(
            "/stt",
            json={"audio": base64.b64encode(b"opus-bytes").decode()},
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json() == {"text": "", "error": "ctranslate2 exploded"}
    assert not Path(model.paths[0]).exists()


def test_stt_bad_base64_never_creates_a_temp_file(monkeypatch):
    model = _FakeWhisper()
    headers = _stt_ready(monkeypatch, model)
    created = []
    real_ntf = voice.tempfile.NamedTemporaryFile

    def recording_ntf(*args, **kwargs):
        tmp = real_ntf(*args, **kwargs)
        created.append(tmp.name)
        return tmp

    monkeypatch.setattr(voice.tempfile, "NamedTemporaryFile", recording_ntf)

    with TestClient(voice.app) as client:
        response = client.post("/stt", json={"audio": "%%%not-base64%%%"}, headers=headers)

    assert response.json()["text"] == ""
    assert "error" in response.json()
    assert created == []
    assert model.paths == []


def test_stt_upload_decodes_in_the_worker_and_cleans_its_temp_file(monkeypatch):
    model = _FakeWhisper(texts=("field", "report"))
    headers = _stt_ready(monkeypatch, model)

    with TestClient(voice.app) as client:
        response = client.post(
            "/stt/upload",
            files={"audio": ("clip.ogg", b"ogg-bytes", "audio/ogg")},
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["text"] == "field report"
    assert model.consumed_on_loop is False
    assert model.paths[0].endswith(".ogg")
    assert not Path(model.paths[0]).exists()


def test_stt_upload_failure_removes_temp_file(monkeypatch):
    model = _FakeWhisper(raise_on_consume=RuntimeError("decode failed"))
    headers = _stt_ready(monkeypatch, model)

    with TestClient(voice.app) as client:
        response = client.post(
            "/stt/upload",
            files={"audio": ("clip.webm", b"webm-bytes", "audio/webm")},
            headers=headers,
        )

    assert response.json() == {"text": "", "error": "decode failed"}
    assert not Path(model.paths[0]).exists()


def test_stt_upload_reads_the_body_before_opening_a_temp_file(monkeypatch):
    """Regression: the upload was read INSIDE the NamedTemporaryFile block,
    so a read that failed left a nameless temp file behind forever."""
    model = _FakeWhisper()
    _stt_ready(monkeypatch, model)  # handler is called directly; no HTTP auth
    created = []
    real_ntf = voice.tempfile.NamedTemporaryFile

    def recording_ntf(*args, **kwargs):
        tmp = real_ntf(*args, **kwargs)
        created.append(tmp.name)
        return tmp

    monkeypatch.setattr(voice.tempfile, "NamedTemporaryFile", recording_ntf)

    class BrokenUpload:
        filename = "clip.webm"

        async def read(self):
            raise RuntimeError("client went away")

    result = asyncio.run(voice.stt_upload(BrokenUpload()))

    assert result == {"text": "", "error": "client went away"}
    assert created == []
    assert model.paths == []


def test_stt_without_a_loaded_model_degrades(monkeypatch):
    headers = _stt_ready(monkeypatch, None)
    with TestClient(voice.app) as client:
        response = client.post("/stt", json={"audio": "AAAA"}, headers=headers)
        upload = client.post(
            "/stt/upload",
            files={"audio": ("clip.webm", b"x", "audio/webm")},
            headers=headers,
        )
    assert response.json() == {"text": "", "error": "Whisper not loaded"}
    assert upload.json() == {"text": "", "error": "Whisper not loaded"}


def test_unlink_quietly_tolerates_blank_and_missing_paths(tmp_path):
    voice._unlink_quietly("")
    voice._unlink_quietly(str(tmp_path / "never-existed.webm"))


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
