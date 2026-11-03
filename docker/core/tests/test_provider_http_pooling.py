"""Provider calls retain transport connections without sharing request settings."""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest

from app import extraction_llm, voice_client

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32
AUDIO = base64.b64encode(b"recorded audio").decode()
MESSAGE = {
    "type": "message",
    "role": "assistant",
    "model": "claude-haiku-5-5",
    "stop_reason": "end_turn",
    "content": [{"type": "text", "text": "selected memory"}],
    "usage": {"input_tokens": 10, "output_tokens": 3},
}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("KLUKAI_VOICE_ENABLED", "true")
    monkeypatch.setenv("KLUKAI_FISH_STT_ENABLED", "true")
    monkeypatch.setenv("FISH_API_KEY", "fish-first-key")
    monkeypatch.delenv("KLUKAI_FISH_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "haiku-first-key")
    monkeypatch.setattr(extraction_llm, "reserve_attempt", AsyncMock(return_value="a"))
    monkeypatch.setattr(extraction_llm, "record_usage", AsyncMock())


def _module(provider):
    return voice_client if provider == "fish" else extraction_llm


async def _call(provider, *, timeout=12.0):
    if provider == "fish":
        response = await voice_client.post_tts({"text": "Speak this."}, timeout=timeout)
        assert response.content == WAV
    else:
        assert await extraction_llm.complete_claude(
            "Recall this.", max_tokens=100, route="test", timeout=timeout
        ) == "selected memory"


def _wire(monkeypatch, handler):
    clients = []
    real = httpx.AsyncClient

    def build(**kwargs):
        client = real(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", build)
    return clients


def _response(request):
    if request.url.path.endswith("/tts"):
        return httpx.Response(200, content=WAV)
    if request.url.path.endswith("/asr"):
        return httpx.Response(200, json={"text": "transcript"})
    return httpx.Response(200, json=MESSAGE)


@pytest.mark.parametrize("provider", ["fish", "haiku"])
async def test_repeated_calls_reuse_client_but_refresh_headers_and_deadlines(
    monkeypatch, provider
):
    requests = []

    def respond(request):
        requests.append(request)
        return _response(request)

    clients = _wire(monkeypatch, respond)
    await _call(provider, timeout=4.0)
    monkeypatch.setenv(
        "FISH_API_KEY" if provider == "fish" else "ANTHROPIC_API_KEY", "rotated-key"
    )
    await _call(provider, timeout=19.0)
    assert len(clients) == 1
    assert not clients[0].is_closed
    header = "authorization" if provider == "fish" else "x-api-key"
    prefix = "Bearer " if provider == "fish" else ""
    assert [request.headers[header] for request in requests] == [
        prefix + ("fish-first-key" if provider == "fish" else "haiku-first-key"),
        prefix + "rotated-key",
    ]
    assert header not in clients[0].headers
    assert [request.extensions["timeout"]["read"] for request in requests] == [4, 19]
    if provider == "haiku":
        assert [r.extensions["timeout"]["connect"] for r in requests] == [5, 5]
        assert extraction_llm.reserve_attempt.await_count == 2
        assert extraction_llm.record_usage.await_count == 2


@pytest.mark.parametrize("provider", ["fish", "haiku"])
async def test_provider_cookies_are_not_retained_across_calls_or_key_rotation(
    monkeypatch, provider
):
    requests = []

    def respond(request):
        requests.append(request)
        response = _response(request)
        response.headers["set-cookie"] = "provider_session=first-key-session; Path=/"
        return response

    clients = _wire(monkeypatch, respond)
    await _call(provider)
    monkeypatch.setenv(
        "FISH_API_KEY" if provider == "fish" else "ANTHROPIC_API_KEY", "rotated-key"
    )
    await _call(provider)
    assert len(clients) == 1
    assert [request.headers.get("cookie") for request in requests] == [None, None]
    assert not clients[0].cookies
    header = "authorization" if provider == "fish" else "x-api-key"
    assert requests[1].headers[header] == (
        "Bearer rotated-key" if provider == "fish" else "rotated-key"
    )


async def test_fish_input_and_output_share_client_without_sharing_model_or_timeout(
    monkeypatch,
):
    requests = []

    def respond(request):
        requests.append(request)
        return _response(request)

    clients = _wire(monkeypatch, respond)
    await _call("fish", timeout=7.0)
    assert await voice_client.transcribe(AUDIO) == {"text": "transcript"}
    await _call("fish", timeout=11.0)
    assert len(clients) == 1
    assert [r.headers["model"] for r in requests] == [
        "s2.1-pro-free", "transcribe-1-pro", "s2.1-pro-free"
    ]
    assert [r.extensions["timeout"]["read"] for r in requests] == [7, 30, 11]


@pytest.mark.parametrize("provider", ["fish", "haiku"])
async def test_concurrent_calls_keep_request_deadlines_separate(monkeypatch, provider):
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def respond(request):
        requests.append(request)
        if len(requests) == 2:
            entered.set()
        await release.wait()
        return _response(request)

    clients = _wire(monkeypatch, respond)
    calls = [asyncio.create_task(_call(provider, timeout=t)) for t in (8, 17)]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert len(clients) == 1
        assert sorted(r.extensions["timeout"]["read"] for r in requests) == [8, 17]
    finally:
        release.set()
        await asyncio.gather(*calls)


@pytest.mark.parametrize("provider", ["fish", "haiku"])
async def test_cancelled_stream_releases_response_without_closing_shared_client(
    monkeypatch, provider
):
    entered = asyncio.Event()

    class WaitingBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b"unreachable"

        async def aclose(self):
            self.closed = True

    body = WaitingBody()
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, stream=body)
        return _response(request)

    clients = _wire(monkeypatch, respond)
    call = asyncio.create_task(
        voice_client.transcribe(AUDIO) if provider == "fish" else _call(provider)
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert body.closed
        assert not clients[0].is_closed
        await _call(provider)
        assert len(clients) == 1 and count == 2
    finally:
        if not call.done():
            call.cancel()
        await asyncio.gather(call, return_exceptions=True)


@pytest.mark.parametrize("provider", ["fish", "haiku"])
async def test_close_releases_client_and_next_lifetime_gets_fresh_client(
    monkeypatch, provider
):
    clients = _wire(monkeypatch, _response)
    await _call(provider)
    assert not clients[0].is_closed
    await _module(provider).close()
    assert clients[0].is_closed
    await _module(provider).close()
    await _call(provider)
    assert len(clients) == 2 and not clients[1].is_closed


@asynccontextmanager
async def _keepalive_server(payload):
    """A real local HTTP/1.1 peer; count accepted TCP connections, not mocks."""
    peers = []
    tasks = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        peers.append(writer.get_extra_info("peername"))
        try:
            while True:
                headers = await reader.readuntil(b"\r\n\r\n")
                length = next(
                    int(line.partition(b":")[2])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                await reader.readexactly(length)
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: "
                    + str(len(payload)).encode()
                    + b"\r\nConnection: keep-alive\r\n\r\n"
                    + payload
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        async with server:
            port = server.sockets[0].getsockname()[1]
            yield f"http://127.0.0.1:{port}", peers
    finally:
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("provider", ["fish", "haiku"])
async def test_provider_reuses_actual_tcp_connection(monkeypatch, provider):
    payload = WAV if provider == "fish" else json.dumps(MESSAGE).encode()
    async with _keepalive_server(payload) as (url, peers):
        module = _module(provider)
        monkeypatch.setattr(
            module, "FISH_TTS_URL" if provider == "fish" else "MESSAGES_URL", url
        )
        try:
            for _ in range(3):
                await _call(provider)
            assert len(peers) == 1
        finally:
            if hasattr(module, "close"):
                await module.close()
