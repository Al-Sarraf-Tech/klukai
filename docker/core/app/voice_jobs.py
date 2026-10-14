"""Bounded in-process voice jobs for synthesis longer than a proxy request.

The core runs one worker process. A restart discards these short-lived jobs;
clients get a 404 and can explicitly retry instead of replaying stale speech.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .voice_client import LOCAL_TTS_DEADLINE, post_leased_tts

logger = logging.getLogger(__name__)
_TERMINAL = {"completed", "failed", "cancelled"}


class VoiceQueueFull(Exception):
    """The bounded voice queue has no room for this user/request."""


@dataclass
class _Job:
    owner: str
    payload: dict[str, Any]
    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created: float = field(default_factory=time.monotonic)
    status: str = "queued"
    finished: float | None = None
    audio: str | None = None
    task: asyncio.Task | None = None


async def _synthesize(payload: dict[str, Any]) -> str:
    async with httpx.AsyncClient(
        timeout=LOCAL_TTS_DEADLINE, trust_env=False, follow_redirects=False
    ) as client:
        response = await post_leased_tts(client, payload, timeout=LOCAL_TTS_DEADLINE)
        response.raise_for_status()
        return base64.b64encode(response.content).decode("ascii")


class VoiceJobs:
    def __init__(
        self, *, synthesize: Callable[[dict[str, Any]], Awaitable[str]] = _synthesize,
        deadline: float = LOCAL_TTS_DEADLINE, max_outstanding: int = 8,
        terminal_ttl: float = 300.0, max_result_bytes: int = 8 * 1024 * 1024,
    ):
        self._synthesize = synthesize
        self._deadline = deadline
        self._max_outstanding = max_outstanding
        self._terminal_ttl = terminal_ttl
        self._max_result_bytes = max_result_bytes
        self._jobs: dict[str, _Job] = {}
        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=max_outstanding)
        self._worker: asyncio.Task | None = None
        self._closed = False

    def _prune(self) -> None:
        now = time.monotonic()
        terminal = sorted(
            (j for j in self._jobs.values() if j.status in _TERMINAL),
            key=lambda j: j.finished or j.created,
        )
        stored_bytes = sum(len(j.audio or "") for j in terminal)
        retained = len(terminal)
        for job in terminal:
            if now - (job.finished or job.created) > self._terminal_ttl or retained > 32 or stored_bytes > 32 * 1024 * 1024:
                self._jobs.pop(job.job_id, None)
                stored_bytes -= len(job.audio or "")
                retained -= 1

    def submit(self, owner: str, payload: dict[str, Any]) -> dict[str, str]:
        self._prune()
        active = [j for j in self._jobs.values() if j.status not in _TERMINAL]
        if self._closed or len(active) >= self._max_outstanding or sum(j.owner == owner for j in active) >= 2:
            raise VoiceQueueFull()
        job = _Job(owner=owner, payload=payload)
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull as exc:
            raise VoiceQueueFull() from exc
        self._jobs[job.job_id] = job
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._consume(), name="voice-jobs")
        return self._view(job)

    def _owned(self, owner: str, job_id: str) -> _Job:
        self._prune()
        job = self._jobs.get(job_id)
        if job is None or job.owner != owner:
            raise KeyError(job_id)
        return job

    @staticmethod
    def _view(job: _Job) -> dict[str, str]:
        result = {"job_id": job.job_id, "status": job.status}
        if job.audio is not None:
            result["audio"] = job.audio
        if job.status == "failed":
            result["error"] = "Voice service unavailable"
        return result

    def get(self, owner: str, job_id: str) -> dict[str, str]:
        return self._view(self._owned(owner, job_id))

    def _remove_pending(self, job: _Job) -> None:
        # No await: compact pending admission atomically while preserving FIFO.
        # Merely marking a queued job cancelled would keep its bounded Queue
        # slot occupied until the (potentially slow) active synthesis finishes.
        pending = []
        while not self._queue.empty():
            queued = self._queue.get_nowait()
            self._queue.task_done()
            if queued is not job:
                pending.append(queued)
        for queued in pending:
            self._queue.put_nowait(queued)

    async def cancel(self, owner: str, job_id: str) -> dict[str, str]:
        job = self._owned(owner, job_id)
        if job.status not in _TERMINAL:
            job.status, job.finished = "cancelled", time.monotonic()
            if job.task is not None:
                job.task.cancel()
                await asyncio.gather(job.task, return_exceptions=True)
            else:
                self._remove_pending(job)
                job.payload = {}
        # DELETE also releases completed audio immediately.
        job.audio = None
        return self._view(job)

    async def _consume(self) -> None:
        while not self._closed:
            job = await self._queue.get()
            try:
                if job.status == "cancelled":
                    continue
                remaining = self._deadline - (time.monotonic() - job.created)
                if remaining <= 0:
                    raise TimeoutError()
                job.status = "running"
                job.task = asyncio.create_task(self._synthesize(job.payload))
                audio = await asyncio.wait_for(job.task, timeout=remaining)
                if not isinstance(audio, str) or not audio or len(audio) > self._max_result_bytes:
                    raise ValueError("Invalid or oversized voice result")
                job.audio, job.status = audio, "completed"
            except asyncio.CancelledError:
                job.status = "cancelled"
                if self._closed:
                    raise
            except Exception as exc:
                logger.warning("Voice job failed: %s", type(exc).__name__)
                job.status = "failed"
            finally:
                job.finished = time.monotonic()
                job.task = None
                job.payload = {}
                self._queue.task_done()
                self._prune()

    async def close(self) -> None:
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self._jobs.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
