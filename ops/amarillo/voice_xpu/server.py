"""Model-free HTTP shell; the disposable child alone imports Torch/TTS."""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
import hmac
import io
import json
import logging
import math
import os
from pathlib import Path
import signal
import time
from typing import Awaitable, Callable, Literal
import wave

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
MAX_AUDIO_BYTES = 16 * 1024 * 1024
MAX_PROTOCOL_BYTES = (MAX_AUDIO_BYTES * 4 // 3) + 4096


class VoiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Settings:
    token: str = field(default_factory=lambda: os.environ.get("VOICE_API_TOKEN", ""), repr=False)
    worker_python: Path = field(default_factory=lambda: Path(os.environ.get(
        "VOICE_XPU_PYTHON", str(Path.home() / ".cache/klukai/xtts-xpu-venv/bin/python"))))
    model_dir: Path = field(default_factory=lambda: Path(os.environ.get(
        "XTTS_MODEL_DIR", str(Path.home() / ".cache/klukai/xtts-v2/6c2b0d75eae4b7047358e3b6bd9325f857d43f77"))))
    reference: Path = ROOT / "reference/selected-reference.wav"
    artifact_lock: Path = ROOT / "artifacts.lock.json"
    idle_seconds: float = 300.0
    request_seconds: float = 600.0
    poll_seconds: float = 0.25

    def __post_init__(self):
        for name, ceiling in (("idle_seconds", 300), ("request_seconds", 600), ("poll_seconds", 1)):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 < value <= ceiling:
                raise ValueError(f"invalid {name}")


def check_temperatures(hwmon_root: Path = Path("/sys/class/hwmon")) -> None:
    """Fail closed when either CPU/GPU telemetry disappears or reaches 68 C."""
    temperatures: dict[str, list[float]] = {"cpu": [], "gpu": []}
    try:
        for hwmon in hwmon_root.glob("hwmon*"):
            name = (hwmon / "name").read_text().strip()
            group = "cpu" if name == "coretemp" else "gpu" if name in ("i915", "xe") else None
            if group:
                for path in hwmon.glob("temp*_input"):
                    value = float(path.read_text().strip()) / 1000
                    if not math.isfinite(value) or not -20 <= value <= 150:
                        raise ValueError("invalid temperature")
                    temperatures[group].append(value)
    except (OSError, ValueError) as exc:
        raise VoiceError(503, "voice thermal telemetry unavailable") from exc
    if not all(temperatures.values()):
        raise VoiceError(503, "voice requires CPU and Intel GPU thermal telemetry")
    if any(max(values) >= 68 for values in temperatures.values()):
        raise VoiceError(503, "voice thermal limit reached")


def clean_worker_environment() -> dict[str, str]:
    # Never inherit host-injected libraries or a caller's Python import path.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("LD_") and key not in ("PYTHONPATH", "PYTHONHOME", "VOICE_API_TOKEN")}
    environment.update(OMP_NUM_THREADS="2", MKL_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2",
                       PYTHONUNBUFFERED="1", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
                       HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", COQUI_TOS_AGREED="1")
    return environment


class WorkerController:
    def __init__(self, settings: Settings, *, command: tuple[str, ...] | None = None,
                 guard: Callable[[], None] = check_temperatures):
        self.settings = settings
        self.command = command or (str(settings.worker_python), "-u", str(ROOT / "worker.py"),
                                   "--model-dir", str(settings.model_dir), "--reference", str(settings.reference),
                                   "--lock", str(settings.artifact_lock))
        self.guard = guard
        self.process: asyncio.subprocess.Process | None = None
        self.busy = False
        self.ready = False
        self.closing = False
        self.unloading = False
        self.last_use = 0.0
        self._active: asyncio.Task | None = None
        self._stop_lock = asyncio.Lock()

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process else None

    async def _exchange(self, text: str, language: str) -> dict:
        if self.process is None:
            # Shield creation: cancellation must not lose an already-created
            # process before its handle is recorded and its group can be killed.
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=None, start_new_session=True, env=clean_worker_environment(),
                limit=MAX_PROTOCOL_BYTES))
            try:
                self.process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                self.process = await spawning
                raise
        process = self.process
        assert process.stdin and process.stdout
        process.stdin.write((json.dumps({"text": text, "language": language}) + "\n").encode())
        await process.stdin.drain()
        line = await process.stdout.readline()
        if not line:
            raise VoiceError(503, "voice worker exited")
        try:
            result = json.loads(line)
            if not isinstance(result, dict) or result.get("sample_rate") != 24000:
                raise ValueError("invalid worker response")
            encoded = result["audio"]
            if not isinstance(encoded, str) or len(encoded) > MAX_PROTOCOL_BYTES:
                raise ValueError("audio is too large")
            audio = base64.b64decode(encoded, validate=True)
            if len(audio) > MAX_AUDIO_BYTES:
                raise ValueError("audio is too large")
            with wave.open(io.BytesIO(audio), "rb") as wav:
                if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 24000) or wav.getnframes() < 1:
                    raise ValueError("invalid audio format")
                if len(wav.readframes(wav.getnframes())) != wav.getnframes() * 2:
                    raise ValueError("truncated WAV")
            return {"audio": encoded, "sample_rate": 24000}
        except (KeyError, TypeError, ValueError, wave.Error, EOFError) as exc:
            raise VoiceError(503, "voice worker returned invalid audio") from exc

    async def _stop_worker(self) -> None:
        async with self._stop_lock:
            process = self.process
            self.ready = False
            if process is None:
                return
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                pass
            # Kill the entire owned session even if the parent exited first:
            # a surviving descendant must not retain GPU allocations.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await asyncio.wait_for(process.wait(), timeout=5)
            if self.process is process:
                self.process = None
            self.last_use = 0.0

    async def _cleanup(self, exchange: asyncio.Task | None) -> None:
        if exchange:
            exchange.cancel()
            await asyncio.gather(exchange, return_exceptions=True)
        await self._stop_worker()

    async def synthesize(self, text: str, language: str,
                         disconnected: Callable[[], Awaitable[bool]]) -> dict:
        # No await between check and reservation: admission is atomic on this
        # single event loop. Uvicorn must run exactly one shell process.
        if self.closing or self.unloading:
            raise VoiceError(503, "voice is unloading")
        if self.busy:
            raise VoiceError(429, "voice is busy")
        self.busy = True
        self._active = asyncio.current_task()
        exchange = None
        deadline = time.monotonic() + self.settings.request_seconds
        try:
            self.guard()
            exchange = asyncio.create_task(self._exchange(text, language))
            while True:
                self.guard()
                if await disconnected():
                    raise VoiceError(499, "voice request disconnected")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise VoiceError(504, "voice synthesis deadline exceeded")
                done, _ = await asyncio.wait({exchange}, timeout=min(self.settings.poll_seconds, remaining))
                if done:
                    result = exchange.result()
                    # The last native operation could have crossed the thermal
                    # limit immediately before returning its PCM data.
                    self.guard()
                    self.ready = True
                    self.last_use = time.monotonic()
                    return result
        except BaseException:
            cleanup = asyncio.create_task(self._cleanup(exchange))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            raise
        finally:
            self.busy = False
            self._active = None

    async def unload(self) -> None:
        self.unloading = True
        try:
            active = self._active
            if active and active is not asyncio.current_task():
                active.cancel()
                await asyncio.gather(active, return_exceptions=True)
            await self._stop_worker()
        finally:
            self.unloading = False

    async def expire_idle(self) -> None:
        if (self.process and not self.busy and not self.unloading and self.last_use > 0
                and time.monotonic() - self.last_use >= self.settings.idle_seconds):
            await self.unload()

    async def close(self) -> None:
        self.closing = True
        await self.unload()


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    language: Literal["en"] = "en"

    @field_validator("text")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("speakable text required")
        return value


def create_app(settings: Settings | None = None, controller: WorkerController | None = None) -> FastAPI:
    settings = settings or Settings()
    controller = controller or WorkerController(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def idle_loop():
            while True:
                await asyncio.sleep(0.5)
                await controller.expire_idle()
        idle = asyncio.create_task(idle_loop())
        try:
            yield
        finally:
            idle.cancel()
            await asyncio.gather(idle, return_exceptions=True)
            await controller.close()

    app = FastAPI(title="Klukai Amarillo XTTS XPU", lifespan=lifespan)

    def require_token(authorization: str | None = Header(default=None)):
        if not settings.token:
            raise HTTPException(503, "voice API token is not configured")
        if not authorization or not hmac.compare_digest(authorization.encode("utf-8"), f"Bearer {settings.token}".encode("utf-8")):
            raise HTTPException(401, "invalid or missing voice API token")

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "companion-voice-xpu", "tts_engine": "xtts",
                "device": "xpu:0", "tts_ready": controller.ready, "busy": controller.busy,
                "lazy_loading": True, "voice_model_ttl": settings.idle_seconds,
                "sample_rate": 24000, "stt_available": False}

    @app.post("/tts", dependencies=[Depends(require_token)])
    async def tts(payload: TTSRequest, request: Request):
        try:
            return await controller.synthesize(payload.text, payload.language, request.is_disconnected)
        except VoiceError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except asyncio.CancelledError:
            # Explicit unload cancels a running request after reaping its worker.
            if not await request.is_disconnected():
                raise HTTPException(503, "voice request cancelled")
            raise
        except Exception:
            logger.exception("voice worker failed")
            raise HTTPException(503, "voice worker unavailable") from None

    @app.post("/unload", dependencies=[Depends(require_token)])
    async def unload():
        await controller.unload()
        return {"status": "ok", "tts_loaded": False, "tts_unloaded": True}

    return app


app = create_app()
