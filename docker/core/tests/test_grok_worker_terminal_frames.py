"""How core reads the worker's terminal frame after a reply has started.

The worker (scripts/agy_image_worker.py) ends a stream that stopped after its
first delta with either a cancelled ``done`` frame (a higher-priority call took
the CLI) or an ``error`` frame. A cancelled background call must surface as
ExtractionCancelled so the extraction queue puts the job back; a failure must
stay a failure. Neither may look like a clean, complete reply.
"""

from __future__ import annotations

import json
import socket
from unittest.mock import AsyncMock

import httpx
import pytest

from app import extraction_llm as xl
from app import grok_chat
from app.grok_chat import GrokChatCancelled, GrokChatError

CANCELLED = {"type": "done", "cancelled": True, "text": ""}
FAILED = {"type": "error", "error": "grok chat failed"}


def _frames(*frames: dict) -> bytes:
    return b"".join(json.dumps(frame).encode() + b"\n" for frame in frames)


@pytest.fixture
def worker(monkeypatch, tmp_path):
    """A fake worker socket whose body is set per test."""
    token = tmp_path / "token"
    token.write_text("test-private-token")
    token.chmod(0o600)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(tmp_path / "image.sock"))
    monkeypatch.setenv("KLUKAI_AGY_SOCKET", str(tmp_path / "image.sock"))
    monkeypatch.setenv("KLUKAI_AGY_TOKEN_FILE", str(token))
    body = {"content": b""}

    def transport(**kwargs):
        return httpx.MockTransport(
            lambda request: httpx.Response(200, content=body["content"])
        )

    monkeypatch.setattr(grok_chat.httpx, "AsyncHTTPTransport", transport)
    try:
        yield body
    finally:
        sock.close()


async def test_a_cancel_after_the_first_delta_is_a_cancel(worker):
    worker["content"] = _frames({"type": "delta", "text": "Part"}, CANCELLED)
    pieces = []
    with pytest.raises(GrokChatCancelled):
        async for piece in grok_chat.iter_text("persona", "hi", role="background"):
            pieces.append(piece)
    assert pieces == ["Part"]
    with pytest.raises(GrokChatCancelled):
        await grok_chat.complete_turn("persona", "hi", role="background")


async def test_a_failure_after_the_first_delta_is_a_failure_not_a_reply(worker):
    worker["content"] = _frames({"type": "delta", "text": "Part"}, FAILED)
    pieces = []
    with pytest.raises(GrokChatError) as caught:
        async for piece in grok_chat.iter_text("persona", "hi"):
            pieces.append(piece)
    assert not isinstance(caught.value, GrokChatCancelled)
    assert pieces == ["Part"]
    with pytest.raises(GrokChatError) as caught:
        await grok_chat.complete_turn("persona", "hi", role="background")
    assert not isinstance(caught.value, GrokChatCancelled)


async def test_a_preempted_extraction_is_requeued_not_dropped(worker, monkeypatch):
    monkeypatch.setattr(xl, "reserve_attempt", AsyncMock(return_value="attempt-1"))
    monkeypatch.setattr(xl, "record_usage", AsyncMock())
    worker["content"] = _frames({"type": "delta", "text": '{"mo'}, CANCELLED)
    with pytest.raises(xl.ExtractionCancelled):
        await xl.complete("extract", max_tokens=256, route="memory_extraction")
    worker["content"] = _frames({"type": "delta", "text": '{"mo'}, FAILED)
    with pytest.raises(xl.ExtractionLLMError) as caught:
        await xl.complete("extract", max_tokens=256, route="memory_extraction")
    assert not isinstance(caught.value, xl.ExtractionCancelled)
