"""Account-login chat over the worker socket. No API key and no api.x.ai."""

import json
import socket

import httpx
import pytest

from app import grok_chat
from app.grok_chat import GrokChatCancelled, GrokChatError


def _token(tmp_path):
    token = tmp_path / "token"
    token.write_text("test-private-token")
    token.chmod(0o600)
    return token


def _socket(tmp_path):
    path = tmp_path / "image.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    return sock


def _install(monkeypatch, tmp_path, handler):
    sock = _socket(tmp_path)
    monkeypatch.setenv("KLUKAI_AGY_SOCKET", str(tmp_path / "image.sock"))
    monkeypatch.setenv("KLUKAI_AGY_TOKEN_FILE", str(_token(tmp_path)))

    def transport(**kwargs):
        assert kwargs["uds"].endswith("/image.sock")
        assert kwargs["retries"] == 0
        assert kwargs["trust_env"] is False
        return httpx.MockTransport(handler)

    monkeypatch.setattr(grok_chat.httpx, "AsyncHTTPTransport", transport)
    return sock


def _body(**changes):
    payload = {
        "text": "  Hello, Commander.  ",
        "model": grok_chat.MODEL,
        "effort": grok_chat.EFFORT,
        "effort_source": "stream",
    }
    payload.update(changes)
    return json.dumps(payload).encode()


def test_configured_requires_the_socket_and_a_private_token(monkeypatch, tmp_path):
    monkeypatch.setenv("KLUKAI_AGY_SOCKET", str(tmp_path / "missing.sock"))
    monkeypatch.setenv("KLUKAI_AGY_TOKEN_FILE", str(tmp_path / "missing-token"))
    assert grok_chat.configured() is False
    sock = _socket(tmp_path)
    try:
        monkeypatch.setenv("KLUKAI_AGY_SOCKET", str(tmp_path / "image.sock"))
        assert grok_chat.configured() is False
        _token(tmp_path)
        monkeypatch.setenv("KLUKAI_AGY_TOKEN_FILE", str(tmp_path / "token"))
        assert grok_chat.configured() is True
    finally:
        sock.close()


def test_configured_fails_closed_when_the_socket_cannot_be_stated(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("KLUKAI_AGY_SOCKET", str(tmp_path / "image.sock"))
    monkeypatch.setenv("KLUKAI_AGY_TOKEN_FILE", str(tmp_path / "token"))

    def denied(self):
        raise OSError("denied")

    monkeypatch.setattr(grok_chat.Path, "is_socket", denied)
    assert grok_chat.configured() is False


async def test_unconfigured_chat_never_calls_the_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("KLUKAI_AGY_SOCKET", str(tmp_path / "missing.sock"))
    monkeypatch.setenv("KLUKAI_AGY_TOKEN_FILE", str(tmp_path / "missing-token"))
    with pytest.raises(GrokChatError, match="not configured"):
        await grok_chat.complete_turn("persona", "hi")


@pytest.mark.parametrize("source", ["stream", "summary", "flag"])
async def test_complete_turn_accepts_medium_grok_from_the_worker(
    monkeypatch, tmp_path, source
):
    seen = {}

    def handler(request):
        seen["authorization"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        assert request.url.path == "/complete"
        return httpx.Response(200, content=_body(effort_source=source))

    sock = _install(monkeypatch, tmp_path, handler)
    try:
        turn = await grok_chat.complete_turn("persona", "hi", timeout=5)
    finally:
        sock.close()
    assert seen["authorization"] == "Bearer test-private-token"
    assert seen["body"] == {
        "system": "persona",
        "prompt": "hi",
        "role": "chat",
        "effort": "low",
    }
    assert (turn.text, turn.model, turn.effort, turn.effort_source) == (
        "Hello, Commander.",
        grok_chat.MODEL,
        grok_chat.EFFORT,
        source,
    )


async def test_complete_turn_rejects_a_non_200(monkeypatch, tmp_path):
    sock = _install(
        monkeypatch, tmp_path, lambda request: httpx.Response(403, content=b"{}")
    )
    try:
        with pytest.raises(GrokChatError, match="failed"):
            await grok_chat.complete_turn("persona", "hi")
    finally:
        sock.close()


async def test_complete_turn_rejects_malformed_json(monkeypatch, tmp_path):
    sock = _install(
        monkeypatch, tmp_path, lambda request: httpx.Response(200, content=b"not-json")
    )
    try:
        with pytest.raises(GrokChatError, match="malformed"):
            await grok_chat.complete_turn("persona", "hi")
    finally:
        sock.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"text": "   "},
        {"text": None},
        {"model": "grok-4"},
        {"effort": "high"},
        {"effort_source": "guess"},
    ],
)
async def test_complete_turn_rejects_an_unexpected_model_or_effort(
    monkeypatch, tmp_path, changes
):
    sock = _install(
        monkeypatch,
        tmp_path,
        lambda request: httpx.Response(200, content=_body(**changes)),
    )
    try:
        with pytest.raises(GrokChatError, match="unexpected"):
            await grok_chat.complete_turn("persona", "hi")
    finally:
        sock.close()


async def test_complete_turn_stops_on_an_oversized_body(monkeypatch, tmp_path):
    sock = _install(
        monkeypatch,
        tmp_path,
        lambda request: httpx.Response(200, content=b"x" * 1_000_001),
    )
    try:
        with pytest.raises(GrokChatError, match="too large"):
            await grok_chat.complete_turn("persona", "hi")
    finally:
        sock.close()


async def test_complete_turn_reports_a_worker_outage_without_a_paid_fallback(
    monkeypatch, tmp_path
):
    def handler(request):
        raise httpx.ConnectError("worker down")

    sock = _install(monkeypatch, tmp_path, handler)
    try:
        with pytest.raises(GrokChatError, match="unavailable") as caught:
            await grok_chat.complete_turn("persona", "hi")
    finally:
        sock.close()
    assert isinstance(caught.value.__cause__, httpx.ConnectError)


def _frames(*frames: dict | str) -> bytes:
    lines = [b"" if frame == "" else json.dumps(frame).encode() for frame in frames]
    return b"\n".join(lines) + b"\n"


def _done(**changes) -> dict:
    payload = {
        "type": "done",
        "text": "Hi there",
        "model": grok_chat.MODEL,
        "effort": grok_chat.EFFORT,
        "effort_source": "stream",
    }
    payload.update(changes)
    return payload


async def test_iter_events_refuses_an_unconfigured_socket(monkeypatch, tmp_path):
    monkeypatch.setenv("KLUKAI_AGY_SOCKET", str(tmp_path / "missing.sock"))
    monkeypatch.setenv("KLUKAI_AGY_TOKEN_FILE", str(tmp_path / "missing-token"))
    with pytest.raises(GrokChatError, match="not configured"):
        await anext(grok_chat.iter_events("persona", "hi"))


async def test_stream_frames_yield_deltas_then_a_done_turn(monkeypatch, tmp_path):
    body = _frames({"type": "delta", "text": "Hi"}, "", _done(effort_source="summary"))
    sock = _install(
        monkeypatch, tmp_path, lambda request: httpx.Response(200, content=body)
    )
    try:
        pieces = [piece async for piece in grok_chat.iter_text("persona", "hi")]
        turn = await grok_chat.complete_turn("persona", "hi")
    finally:
        sock.close()
    assert pieces == ["Hi"]
    assert (turn.text, turn.effort_source) == ("Hi there", "summary")


async def test_a_cancelled_frame_stops_streaming_and_the_turn(monkeypatch, tmp_path):
    body = _frames({"cancelled": True})
    sock = _install(
        monkeypatch, tmp_path, lambda request: httpx.Response(200, content=body)
    )
    try:
        with pytest.raises(GrokChatCancelled, match="cancelled"):
            async for _piece in grok_chat.iter_text("persona", "hi"):
                pass
        with pytest.raises(GrokChatCancelled, match="cancelled"):
            await grok_chat.complete_turn("persona", "hi")
    finally:
        sock.close()


async def test_a_stream_without_a_done_frame_returns_no_text(monkeypatch, tmp_path):
    body = _frames({"type": "delta", "text": 1})
    sock = _install(
        monkeypatch, tmp_path, lambda request: httpx.Response(200, content=body)
    )
    try:
        assert [piece async for piece in grok_chat.iter_text("persona", "hi")] == []
        with pytest.raises(GrokChatError, match="no text"):
            await grok_chat.complete_turn("persona", "hi")
    finally:
        sock.close()


async def test_recall_does_not_share_chats_breaker(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager

    deps = []
    real = grok_chat.http_stream

    @asynccontextmanager
    async def wrapped(dep, *args, **kwargs):
        deps.append(dep)
        async with real(dep, *args, **kwargs) as response:
            yield response

    monkeypatch.setattr(grok_chat, "http_stream", wrapped)
    sock = _install(
        monkeypatch, tmp_path, lambda request: httpx.Response(200, content=_body())
    )
    try:
        await grok_chat.complete_turn("persona", "hi", role="chat")
        await grok_chat.complete_turn("persona", "hi", role="recall")
    finally:
        sock.close()
    assert deps == ["grok_chat", "grok_recall"]
    assert grok_chat.breaker_name("background") == "grok_chat"
