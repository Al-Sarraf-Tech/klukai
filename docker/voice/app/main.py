"""Companion Voice: XTTS v2 voice-cloned TTS + faster-whisper STT."""

from __future__ import annotations

import asyncio
import base64
import gc
import hmac
import logging
import os
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import Response

from .audio import (
    transcribe_sync as _transcribe_sync,
    unlink_quietly as _unlink_quietly,
)
from .lease_validation import require_xtts_gpu_lease
from .requests import (
    TTSRequest as TTSRequest,
    STTRequest as STTRequest,
    MAX_STT_AUDIO_B64 as MAX_STT_AUDIO_B64,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

REFERENCE_WAV = os.environ.get("REFERENCE_WAV", "/app/reference/klukai_reference.wav")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base.en")
MODEL_DIR = Path("/app/models")
WHISPER_MODEL_DIR = Path(
    os.environ.get("WHISPER_MODEL_DIR", str(MODEL_DIR / "whisper-base-en"))
)
XTTS_MODEL_DIR = Path(os.environ.get("XTTS_MODEL_DIR", str(MODEL_DIR / "xtts-v2")))
TTS_ENGINE = os.environ.get("TTS_ENGINE", "xtts")


MAX_MODEL_IDLE_TTL_SECONDS = 15 * 60
# The unloader scans twice per second.  A server-owned five-second margin keeps
# the latest scheduled scan below the literal 900-second residency ceiling,
# even when an operator requests the full public maximum.
MODEL_IDLE_SAFETY_CUTOFF_SECONDS = 895
MODEL_IDLE_SCAN_INTERVAL_SECONDS = 0.5


def _bounded_ttl_env(name: str, default: int) -> int:
    try:
        configured = int(os.environ.get(name, str(default)))
        return min(MAX_MODEL_IDLE_TTL_SECONDS, max(1, configured))
    except ValueError:
        logger.warning("Invalid %s; using %d", name, default)
        return default


VOICE_MODEL_TTL = min(
    _bounded_ttl_env("VOICE_MODEL_TTL", 600),
    MODEL_IDLE_SAFETY_CUTOFF_SECONDS,
)

# Bearer auth is mandatory for every model-loading endpoint. The canonical
# Compose deployment also refuses to render without this secret.
VOICE_API_TOKEN = os.environ.get("VOICE_API_TOKEN", "")
GPU_LEASE_MARKER = Path(
    os.environ.get("GPU_LEASE_MARKER", "/run/dominus-gpu/non-llm-lease.json")
)
GPU_LEASE_HEADER = "X-GPU-Lease-Token"


def require_token(authorization: str | None = Header(default=None)) -> None:
    if not VOICE_API_TOKEN:
        raise HTTPException(status_code=503, detail="voice API token is not configured")
    expected = f"Bearer {VOICE_API_TOKEN}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=401, detail="invalid or missing voice API token"
        )


def _require_xtts_gpu_lease(token: str | None, now: float | None = None) -> None:
    require_xtts_gpu_lease(token, GPU_LEASE_MARKER, now)


_tts_model: Any = None
_whisper_model: Any = None
_tts_ready = False
_last_tts_use = 0.0
_last_whisper_use = 0.0
_tts_access_lock = asyncio.Lock()
_whisper_access_lock = asyncio.Lock()


def _load_xtts() -> Any:
    global _tts_model, _tts_ready
    if _tts_model is not None:
        return _tts_model
    try:
        os.environ["COQUI_TOS_AGREED"] = "1"
        # XTTS checkpoints use pickle serialization — override weights_only default
        # This is safe: we're loading the official Coqui XTTS v2 model from HuggingFace
        import torch

        _orig_load = torch.load

        def _patched_load(*a: Any, **kw: Any) -> Any:
            kw["weights_only"] = False
            return _orig_load(*a, **kw)

        # Scope the unsafe-pickle override to ONLY the trusted XTTS checkpoint
        # load, then restore torch's safe default so weights_only=False can't
        # silently apply to any later (potentially untrusted) torch.load call.
        torch.load = _patched_load
        try:
            from TTS.api import TTS

            logger.info("Loading XTTS v2 model...")
            required_files = (
                "config.json",
                "dvae.pth",
                "mel_stats.pth",
                "model.pth",
                "speakers_xtts.pth",
                "vocab.json",
            )
            missing = [
                filename
                for filename in required_files
                if not (XTTS_MODEL_DIR / filename).is_file()
            ]
            if missing:
                raise FileNotFoundError(
                    f"pinned XTTS snapshot is incomplete at {XTTS_MODEL_DIR}: "
                    + ", ".join(missing)
                )
            config_path = XTTS_MODEL_DIR / "config.json"
            logger.info("Using pinned XTTS snapshot at %s", XTTS_MODEL_DIR)
            _tts_model = TTS(
                # Coqui's TTS() joins model_path with "model.pth" itself, so
                # this must be the snapshot directory, not the checkpoint file.
                model_path=str(XTTS_MODEL_DIR),
                config_path=str(config_path),
                progress_bar=False,
                gpu=True,
            )
            # Coqui 0.22's explicit-checkpoint path uses the input sample rate
            # for WAV output. XTTS's decoder emits output_sample_rate instead;
            # label those samples correctly without resampling or pitch shifts.
            synthesizer = _tts_model.synthesizer
            synthesizer.output_sample_rate = synthesizer.tts_config.audio[
                "output_sample_rate"
            ]
        finally:
            torch.load = _orig_load
        _tts_ready = True
        logger.info("XTTS v2 loaded (GPU)")
        return _tts_model
    except Exception as e:
        logger.error("Failed to load XTTS v2: %s", e)
        return None


def _unload_xtts() -> bool:
    """Release XTTS and its CUDA allocations.

    The async caller serializes this with synthesis using ``_tts_access_lock``.
    """
    global _tts_model, _tts_ready
    model = _tts_model
    had_model = model is not None
    if model is not None:
        try:
            if hasattr(model, "to"):
                model.to("cpu")
            elif getattr(model, "synthesizer", None) is not None:
                tts_model = getattr(model.synthesizer, "tts_model", None)
                if tts_model is not None and hasattr(tts_model, "to"):
                    tts_model.to("cpu")
        except Exception as exc:
            # Retain the reference so a retry or operator restart can still
            # quiesce it. The gateway keeps the lease marker fail-closed.
            logger.error("XTTS CPU handoff during unload failed: %s", exc)
            return False
        _tts_model = None
        _tts_ready = False
        del model
    else:
        _tts_ready = False

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except ModuleNotFoundError as exc:
        if had_model:
            logger.error("CUDA cleanup runtime is unavailable: %s", exc)
            return False
    except Exception as exc:
        logger.error("CUDA cache cleanup during XTTS unload failed: %s", exc)
        return False
    logger.info("XTTS v2 unloaded")
    return True


def _load_whisper() -> Any:
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    try:
        from faster_whisper import WhisperModel

        whisper_dir = MODEL_DIR / "whisper"
        whisper_dir.mkdir(parents=True, exist_ok=True)
        required_files = (
            "config.json",
            "model.bin",
            "tokenizer.json",
            "vocabulary.txt",
        )
        missing = [
            filename
            for filename in required_files
            if not (WHISPER_MODEL_DIR / filename).is_file()
        ]
        if missing:
            raise FileNotFoundError(
                f"pinned Whisper snapshot is incomplete at {WHISPER_MODEL_DIR}: "
                + ", ".join(missing)
            )
        _whisper_model = WhisperModel(
            str(WHISPER_MODEL_DIR),
            device="cpu",
            compute_type="int8",
            download_root=str(whisper_dir),
        )
        logger.info("Whisper loaded: %s", WHISPER_MODEL)
        return _whisper_model
    except Exception as e:
        logger.error("Failed to load Whisper: %s", e)
        return None


def _unload_whisper() -> bool:
    """Release the CPU Whisper model and its resident memory."""
    global _whisper_model
    if _whisper_model is None:
        return False
    model = _whisper_model
    _whisper_model = None
    del model
    gc.collect()
    logger.info("Whisper unloaded")
    return True


async def _unload_expired_models(now: float) -> None:
    """Run one deterministic idle-deadline scan for both resident models."""
    if (
        _tts_model is not None
        and _last_tts_use > 0
        and now - _last_tts_use >= VOICE_MODEL_TTL
    ):
        async with _tts_access_lock:
            if (
                _tts_model is not None
                and _last_tts_use > 0
                and time.monotonic() - _last_tts_use >= VOICE_MODEL_TTL
            ):
                await asyncio.to_thread(_unload_xtts)
    if (
        _whisper_model is not None
        and _last_whisper_use > 0
        and now - _last_whisper_use >= VOICE_MODEL_TTL
    ):
        async with _whisper_access_lock:
            if (
                _whisper_model is not None
                and _last_whisper_use > 0
                and time.monotonic() - _last_whisper_use >= VOICE_MODEL_TTL
            ):
                await asyncio.to_thread(_unload_whisper)


async def _idle_unloader() -> None:
    while True:
        await asyncio.sleep(MODEL_IDLE_SCAN_INTERVAL_SECONDS)
        await _unload_expired_models(time.monotonic())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Health probes and container restarts must not consume GPU memory. The
    # first real TTS/STT request loads its model from the pinned snapshot.
    idle_task = asyncio.create_task(_idle_unloader())
    logger.info(
        "Voice service started (engine: %s, lazy models, XTTS TTL: %ss)",
        TTS_ENGINE,
        VOICE_MODEL_TTL,
    )
    try:
        yield
    finally:
        idle_task.cancel()
        try:
            await idle_task
        except asyncio.CancelledError:
            pass
        async with _tts_access_lock:
            await asyncio.to_thread(_unload_xtts)
        async with _whisper_access_lock:
            await asyncio.to_thread(_unload_whisper)


app = FastAPI(title="Companion Voice", version="0.2.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str | bool | float]:
    return {
        "status": "ok",
        "service": "companion-voice",
        "tts_engine": TTS_ENGINE,
        "tts_ready": _tts_ready,
        "whisper_loaded": _whisper_model is not None,
        "lazy_loading": True,
        "voice_model_ttl": VOICE_MODEL_TTL,
    }


@app.post("/unload", dependencies=[Depends(require_token)])
async def unload_models() -> dict[str, str | bool]:
    """Explicitly release all resident voice models for the shared guard."""
    async with _tts_access_lock:
        tts_unloaded = await asyncio.to_thread(_unload_xtts)
    async with _whisper_access_lock:
        whisper_unloaded = await asyncio.to_thread(_unload_whisper)
    if not tts_unloaded or _tts_model is not None or _tts_ready:
        raise HTTPException(
            status_code=503, detail="XTTS unload could not be confirmed"
        )
    return {
        "status": "ok",
        "tts_unloaded": tts_unloaded,
        "whisper_unloaded": whisper_unloaded,
        "tts_loaded": False,
        "whisper_loaded": _whisper_model is not None,
    }


@app.post("/tts", dependencies=[Depends(require_token)])
async def text_to_speech(
    req: TTSRequest,
    x_gpu_lease_token: str | None = Header(default=None, alias=GPU_LEASE_HEADER),
) -> Response:
    """Convert text to speech using XTTS v2 voice cloning."""
    global _last_tts_use
    ref = Path(REFERENCE_WAV)
    if not ref.exists():
        return Response(content=b"Reference WAV not found", status_code=404)

    async with _tts_access_lock:
        # Validate after taking the same lock used by gateway-owned unload.
        # Once release marks the lease "cleaning", no queued TTS request can
        # slip in ahead of cleanup and reload XTTS.
        _require_xtts_gpu_lease(x_gpu_lease_token)
        model = await asyncio.to_thread(_load_xtts)
        if model is None:
            return Response(content=b"XTTS not loaded", status_code=503)

        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name

            synthesis = asyncio.create_task(
                asyncio.to_thread(
                    model.tts_to_file,
                    text=req.text,
                    speaker_wav=str(ref),
                    language=req.language,
                    file_path=tmp_path,
                )
            )
            try:
                await asyncio.shield(synthesis)
            except asyncio.CancelledError:
                # A disconnected caller must not release the access lock while
                # the native synthesis thread still owns CUDA state. Gateway
                # expiry remains fail-closed and waits for this to finish.
                try:
                    await asyncio.shield(synthesis)
                except Exception as exc:
                    logger.error("Cancelled TTS worker failed during drain: %s", exc)
                raise

            with open(tmp_path, "rb") as f:
                wav_data = f.read()
            return Response(content=wav_data, media_type="audio/wav")
        except Exception as e:
            logger.error("TTS failed: %s", e)
            return Response(content=f"TTS failed: {e}".encode(), status_code=500)
        finally:
            _last_tts_use = time.monotonic()
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass


@app.post("/stt", dependencies=[Depends(require_token)])
async def speech_to_text(req: STTRequest) -> dict[str, Any]:
    """Convert speech to text using faster-whisper."""
    global _last_whisper_use
    async with _whisper_access_lock:
        model = await asyncio.to_thread(_load_whisper)
        if model is None:
            return {"text": "", "error": "Whisper not loaded"}

        tmp_path = ""
        try:
            audio_bytes = base64.b64decode(req.audio)
            # Record the name before writing so a failed write still unlinks.
            with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
                tmp_path = tmp.name
                tmp.write(audio_bytes)
            text, info = await asyncio.to_thread(_transcribe_sync, model, tmp_path)
            return {
                "text": text,
                "language": info.language,
                "duration": info.duration,
            }
        except Exception as e:
            logger.error("STT failed: %s", e)
            return {"text": "", "error": str(e)}
        finally:
            _last_whisper_use = time.monotonic()
            _unlink_quietly(tmp_path)


_AUDIO_UPLOAD = File(...)


@app.post("/stt/upload", dependencies=[Depends(require_token)])
async def stt_upload(audio: UploadFile = _AUDIO_UPLOAD) -> dict[str, Any]:
    """STT from file upload."""
    global _last_whisper_use
    async with _whisper_access_lock:
        model = await asyncio.to_thread(_load_whisper)
        if model is None:
            return {"text": "", "error": "Whisper not loaded"}

        tmp_path = ""
        try:
            # Read the body BEFORE creating the temp file: an upload that fails
            # mid-read used to leave a nameless temp file behind forever.
            content = await audio.read()
            suffix = f".{audio.filename.split('.')[-1] if audio.filename else 'webm'}"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = tmp.name
                tmp.write(content)
            text, info = await asyncio.to_thread(_transcribe_sync, model, tmp_path)
            return {
                "text": text,
                "language": info.language,
                "duration": info.duration,
            }
        except Exception as e:
            logger.error("STT upload failed: %s", e)
            return {"text": "", "error": str(e)}
        finally:
            _last_whisper_use = time.monotonic()
            _unlink_quietly(tmp_path)
