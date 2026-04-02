"""Companion Voice: Piper TTS + faster-whisper STT service."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import subprocess
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

PIPER_VOICE = os.environ.get("PIPER_VOICE", "en_US-amy-medium")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base.en")
MODEL_DIR = Path("/app/models")

# Globals
_whisper_model = None


def _ensure_piper_voice() -> Path:
    """Download Piper voice model if not present."""
    voice_dir = MODEL_DIR / "piper"
    voice_dir.mkdir(parents=True, exist_ok=True)
    onnx_file = voice_dir / f"{PIPER_VOICE}.onnx"
    json_file = voice_dir / f"{PIPER_VOICE}.onnx.json"

    if onnx_file.exists() and json_file.exists():
        return onnx_file

    logger.info("Downloading Piper voice: %s", PIPER_VOICE)
    base_url = f"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/{PIPER_VOICE.replace('-', '/')}"

    for fname, dest in [(f"{PIPER_VOICE}.onnx", onnx_file), (f"{PIPER_VOICE}.onnx.json", json_file)]:
        # Construct the actual HuggingFace URL pattern for piper voices
        quality = PIPER_VOICE.rsplit("-", 1)[-1]  # e.g., "medium"
        lang = PIPER_VOICE.split("-")[0] + "_" + PIPER_VOICE.split("-")[1]  # e.g., "en_US"
        name = "-".join(PIPER_VOICE.split("-")[2:-1])  # e.g., "amy"
        url = f"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/{lang.replace('_', '/')}/{name}/{quality}/{fname}"

        subprocess.run(
            ["curl", "-sL", "-o", str(dest), url],
            check=True,
            timeout=120,
        )
    logger.info("Piper voice downloaded: %s", onnx_file)
    return onnx_file


def _load_whisper():
    """Load faster-whisper model."""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model

    try:
        from faster_whisper import WhisperModel
        whisper_dir = MODEL_DIR / "whisper"
        whisper_dir.mkdir(parents=True, exist_ok=True)
        _whisper_model = WhisperModel(
            WHISPER_MODEL,
            device="cpu",
            compute_type="int8",
            download_root=str(whisper_dir),
        )
        logger.info("Whisper model loaded: %s", WHISPER_MODEL)
        return _whisper_model
    except Exception as e:
        logger.error("Failed to load Whisper: %s", e)
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_piper_voice()
    _load_whisper()
    logger.info("Companion voice service started")
    yield
    logger.info("Companion voice service stopped")


app = FastAPI(title="Companion Voice", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "companion-voice",
        "piper_voice": PIPER_VOICE,
        "whisper_model": WHISPER_MODEL,
        "whisper_loaded": _whisper_model is not None,
    }


# ── TTS ──────────────────────────────────────────────────────────────────────


class TTSRequest(BaseModel):
    text: str
    voice: str | None = None


@app.post("/tts")
async def text_to_speech(req: TTSRequest):
    """Convert text to speech using Piper. Returns WAV audio."""
    voice = req.voice or PIPER_VOICE
    onnx_path = MODEL_DIR / "piper" / f"{voice}.onnx"

    if not onnx_path.exists():
        return Response(content=b"Voice model not found", status_code=404)

    try:
        proc = await asyncio.create_subprocess_exec(
            "piper",
            "--model", str(onnx_path),
            "--output-raw",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=req.text.encode()),
            timeout=30.0,
        )

        if proc.returncode != 0:
            logger.error("Piper failed: %s", stderr.decode())
            return Response(content=b"TTS failed", status_code=500)

        # Raw PCM -> WAV header
        import struct
        sample_rate = 22050
        num_channels = 1
        bits_per_sample = 16
        data_size = len(stdout)
        wav_header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36 + data_size, b"WAVE",
            b"fmt ", 16, 1, num_channels,
            sample_rate, sample_rate * num_channels * bits_per_sample // 8,
            num_channels * bits_per_sample // 8, bits_per_sample,
            b"data", data_size,
        )
        wav_data = wav_header + stdout

        return Response(content=wav_data, media_type="audio/wav")
    except asyncio.TimeoutError:
        return Response(content=b"TTS timeout", status_code=504)
    except FileNotFoundError:
        # Piper not installed, return error
        return Response(content=b"Piper not installed", status_code=503)


# ── STT ──────────────────────────────────────────────────────────────────────


class STTRequest(BaseModel):
    audio: str  # base64-encoded audio


@app.post("/stt")
async def speech_to_text(req: STTRequest):
    """Convert speech to text using faster-whisper."""
    model = _load_whisper()
    if model is None:
        return {"text": "", "error": "Whisper model not loaded"}

    try:
        audio_bytes = base64.b64decode(req.audio)

        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            segments, info = model.transcribe(tmp_path, beam_size=5)
            text = " ".join(s.text for s in segments).strip()
            return {
                "text": text,
                "language": info.language,
                "duration": info.duration,
            }
        finally:
            os.unlink(tmp_path)
    except Exception as e:
        logger.error("STT failed: %s", e)
        return {"text": "", "error": str(e)}


# ── File upload STT (alternative) ───────────────────────────────────────────


@app.post("/stt/upload")
async def stt_upload(audio: UploadFile = File(...)):
    """STT from file upload."""
    model = _load_whisper()
    if model is None:
        return {"text": "", "error": "Whisper model not loaded"}

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
            return {
                "text": text,
                "language": info.language,
                "duration": info.duration,
            }
        finally:
            os.unlink(tmp_path)
    except Exception as e:
        logger.error("STT upload failed: %s", e)
        return {"text": "", "error": str(e)}
