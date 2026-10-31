"""A streamed chat reply that succeeds while the breaker is half-open closes it.

The chat reply only ever uses iter_text. If iter_text left iter_events
suspended after ``done``, the probe ended in GeneratorExit (neither success
nor failure) and the grok_chat breaker stayed half-open.
"""

from __future__ import annotations

import json
import socket
import time

import httpx
import pytest

from app import circuit_breakers as cb
from app import grok_chat
from app.circuit_breakers import State, get_breaker


@pytest.fixture(autouse=True)
def isolated_breakers():
    cb._BREAKERS.clear()
    yield
    cb._BREAKERS.clear()

DONE = {
    "type": "done",
    "text": "Hi there",
    "model": grok_chat.MODEL,
    "effort": grok_chat.EFFORT,
    "effort_source": "stream",
}


@pytest.fixture
def worker(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("test-private-token")
    token.chmod(0o600)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(tmp_path / "image.sock"))
    monkeypatch.setenv("KLUKAI_AGY_SOCKET", str(tmp_path / "image.sock"))
    monkeypatch.setenv("KLUKAI_AGY_TOKEN_FILE", str(token))
    body = b"".join(
        json.dumps(frame).encode() + b"\n"
        for frame in (
            {"type": "delta", "text": "Hi "},
            {"type": "delta", "text": "there"},
            DONE,
            # A frame after done must be drained, not left unread inside the breaker.
            {"type": "delta", "text": "late"},
        )
    )
    monkeypatch.setattr(
        grok_chat.httpx,
        "AsyncHTTPTransport",
        lambda **kwargs: httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
    )
    try:
        yield
    finally:
        sock.close()


def _due_for_a_probe(name: str):
    breaker = get_breaker(name)
    breaker.force_open()
    breaker._last_probe = time.monotonic() - breaker.cfg.probe_interval_seconds - 1
    return breaker


async def test_a_streamed_reply_closes_a_half_open_breaker(worker):
    breaker = _due_for_a_probe("grok_chat")
    pieces = [piece async for piece in grok_chat.iter_text("persona", "hi")]
    assert pieces == ["Hi ", "there"]
    assert breaker.state is State.CLOSED
    assert not breaker._half_open_lock.locked()


async def test_a_streamed_recall_closes_its_own_breaker(worker):
    breaker = _due_for_a_probe("grok_recall")
    assert [p async for p in grok_chat.iter_text("rank", "q", role="recall")] == ["Hi ", "there"]
    assert breaker.state is State.CLOSED


async def test_a_streamed_background_call_closes_its_own_breaker(worker):
    breaker = _due_for_a_probe("grok_background")
    pieces = [p async for p in grok_chat.iter_text("extract", "q", role="background")]
    assert pieces == ["Hi ", "there"]
    assert breaker.state is State.CLOSED
