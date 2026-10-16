"""Synchronous audio decoding and temporary-file cleanup primitives."""

import os
from typing import Any


def transcribe_sync(model: Any, path: str) -> tuple[str, Any]:
    """Run faster-whisper to completion on the calling (worker) thread.

    ``transcribe()`` only prepares a lazy generator; the actual decoding runs
    while the segments are consumed. Joining them here keeps that CPU-bound
    work inside the ``asyncio.to_thread`` worker. Joining on the event loop
    froze /health and /tts for the length of the clip.
    """
    segments, info = model.transcribe(path, beam_size=5)
    text = " ".join(s.text for s in segments).strip()
    return text, info


def unlink_quietly(path: str) -> None:
    if path:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
