"""Companion Voice: XTTS v2 voice-cloned TTS + faster-whisper STT."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

REFERENCE_WAV = os.environ.get("REFERENCE_WAV", "/app/reference/klukai_reference.wav")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base.en")
MODEL_DIR = Path("/app/models")
TTS_ENGINE = os.environ.get("TTS_ENGINE", "xtts")

_tts_model = None
_whisper_model = None
_tts_ready = False


def _load_xtts():
    global _tts_model, _tts_ready
    if _tts_model is not None:
        return _tts_model
    try:
        os.environ["COQUI_TOS_AGREED"] = "1"
        # XTTS checkpoints use pickle serialization — override weights_only default
        # This is safe: we're loading the official Coqui XTTS v2 model from HuggingFace
        import torch
        _orig_load = torch.load
        def _patched_load(*a, **kw):
            kw["weights_only"] = False
            return _orig_load(*a, **kw)
        torch.load = _patched_load
        from TTS.api import TTS
        logger.info("Loading XTTS v2 model...")
        _tts_model = TTS(
            model_name="tts_models/multilingual/multi-dataset/xtts_v2",
            gpu=True,
        )
        _tts_ready = True
        logger.info("XTTS v2 loaded (GPU)")
        return _tts_model
    except Exception as e:
        logger.error("Failed to load XTTS v2: %s", e)
        return None


def _load_whisper():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    try:
        from faster_whisper import WhisperModel
        whisper_dir = MODEL_DIR / "whisper"
        whisper_dir.mkdir(parents=True, exist_ok=True)
        _whisper_model = WhisperModel(
            WHISPER_MODEL, device="cpu", compute_type="int8",
            download_root=str(whisper_dir),
        )
        logger.info("Whisper loaded: %s", WHISPER_MODEL)
        return _whisper_model
    except Exception as e:
        logger.error("Failed to load Whisper: %s", e)
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    if TTS_ENGINE == "xtts":
        _load_xtts()
    _load_whisper()
    logger.info("Voice service started (engine: %s)", TTS_ENGINE)
    yield


app = FastAPI(title="Companion Voice", version="0.2.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "companion-voice",
        "tts_engine": TTS_ENGINE,
        "tts_ready": _tts_ready,
        "whisper_loaded": _whisper_model is not None,
    }


class TTSRequest(BaseModel):
    text: str
    language: str = "en"


@app.post("/tts")
async def text_to_speech(req: TTSRequest):
    """Convert text to speech using XTTS v2 voice cloning."""
    model = _load_xtts()
    if model is None:
        return Response(content=b"XTTS not loaded", status_code=503)

    ref = Path(REFERENCE_WAV)
    if not ref.exists():
        return Response(content=b"Reference WAV not found", status_code=404)

    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: model.tts_to_file(
                text=req.text,
                speaker_wav=str(ref),
                language=req.language,
                file_path=tmp_path,
            ),
        )

        with open(tmp_path, "rb") as f:
            wav_data = f.read()
        os.unlink(tmp_path)

        return Response(content=wav_data, media_type="audio/wav")
    except Exception as e:
        logger.error("TTS failed: %s", e)
        return Response(content=f"TTS failed: {e}".encode(), status_code=500)


class STTRequest(BaseModel):
    audio: str


@app.post("/stt")
async def speech_to_text(req: STTRequest):
    """Convert speech to text using faster-whisper."""
    model = _load_whisper()
    if model is None:
        return {"text": "", "error": "Whisper not loaded"}

    try:
        audio_bytes = base64.b64decode(req.audio)
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        try:
            segments, info = model.transcribe(tmp_path, beam_size=5)
            text = " ".join(s.text for s in segments).strip()
            return {"text": text, "language": info.language, "duration": info.duration}
        finally:
            os.unlink(tmp_path)
    except Exception as e:
        logger.error("STT failed: %s", e)
        return {"text": "", "error": str(e)}


@app.post("/stt/upload")
async def stt_upload(audio: UploadFile = File(...)):
    """STT from file upload."""
    model = _load_whisper()
    if model is None:
        return {"text": "", "error": "Whisper not loaded"}

    try:
        with tempfile.NamedTemporaryFile(
            suffix=f".{audio.filename.split('.')[-1] if audio.filename else 'webm'}",
            delete=False,
        ) as tmp:
            content = await audio.read()
            tmp.write(content)
            tmp_path = tmp.name
        try:
            segments, info = model.transcribe(tmp_path, beam_size=5)
            text = " ".join(s.text for s in segments).strip()
            return {"text": text, "language": info.language, "duration": info.duration}
        finally:
            os.unlink(tmp_path)
    except Exception as e:
        logger.error("STT upload failed: %s", e)
        return {"text": "", "error": str(e)}
