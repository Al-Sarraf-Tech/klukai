"""Real HTTP framing for native completion limits, EOF, and cancellation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app import llm_router as routing
from app.models import LLMConfig

MESSAGES = [{"role": "user", "content": "hello"}]


@pytest.fixture
async def connect():
    clients = []

    def make(handler):
        router = routing.LLMRouter()
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        router._anthropic_http = client
        return router

    yield make
    for client in clients:
        await client.aclose()


def native_reply(**changes):
    return {
        "model": "fixture-native-model",
        "content": [{"type": "text", "text": "public reply"}],
        "stop_reason": "end_turn",
        **changes,
    }


async def test_unconfigured_primary_rejects_before_any_transport():
    router = routing.LLMRouter()
    config = LLMConfig(provider="anthropic", model="selected")
    with pytest.raises(ValueError, match="primary is unavailable"):
        await router._complete_anthropic("persona", MESSAGES, config)
    with pytest.raises(ValueError, match="Unavailable LLM provider"):
        await router.complete_chat("persona", MESSAGES, config)


@pytest.mark.parametrize("model", [None, "", 17])
async def test_native_model_identity_is_required_before_config_mutation(connect, model):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=native_reply(model=model))

    router = connect(handler)
    config = LLMConfig(provider="anthropic", model="selected")
    with pytest.raises(ValueError, match="Missing response model"):
        await router._complete_anthropic("persona", MESSAGES, config)
    assert config.model == "selected" and len(requests) == 1


async def test_native_body_limit_stops_before_exposing_text(connect, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json=native_reply(content=[{"type": "text", "text": "private" * 100}])
        )

    router = connect(handler)
    monkeypatch.setattr(routing, "_ANTHROPIC_MAX_RESPONSE_BYTES", 64)
    config = LLMConfig(provider="anthropic", model="selected")
    with pytest.raises(ValueError, match="exceeds size limit"):
        await router._complete_anthropic("persona", MESSAGES, config)
    assert config.model == "selected" and len(requests) == 1
    assert router._anthropic_gate._value == 2


async def test_broken_usage_metadata_does_not_discard_valid_public_answer(connect):
    router = connect(
        lambda request: httpx.Response(
            200, json=native_reply(usage={"input_tokens": "not an integer"})
        )
    )
    config = LLMConfig(provider="anthropic", model="selected")
    result = await router._complete_anthropic("persona", MESSAGES, config)
    assert result["choices"][0]["message"]["content"] == "public reply"
    assert result["model"] == config.model == "fixture-native-model"


async def test_disconnect_cancels_native_stream_and_releases_admission_without_fallback(
    connect, monkeypatch
):
    entered, closed = asyncio.Event(), asyncio.Event()

    class BlockedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b"never emitted"

        async def aclose(self):
            closed.set()

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=BlockedBody())

    router = connect(handler)
    fallback, failed = AsyncMock(), Mock()
    monkeypatch.setattr(router, "_select_local_secondary", fallback)
    monkeypatch.setattr(router, "_primary_failed", failed)
    task = asyncio.create_task(
        router.complete_chat(
            "persona", MESSAGES, LLMConfig(provider="anthropic", model="selected")
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set() and router._anthropic_gate._value == 2
        assert len(requests) == 1
        fallback.assert_not_awaited()
        failed.assert_not_called()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_local_eof_without_done_or_content_never_invents_a_reply(
    connect, monkeypatch
):
    from app import observability

    record = Mock()
    monkeypatch.setattr(observability, "record_llm_usage", record)
    router = connect(
        lambda request: httpx.Response(
            200, content=b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n'
        )
    )
    router._http = router._anthropic_http
    config = LLMConfig(
        provider="lmstudio", model="fixture-local", base_url="http://local.invalid"
    )
    assert [
        text async for text in router._stream_lmstudio("persona", MESSAGES, config)
    ] == []
    record.assert_called_once()
    assert record.call_args.kwargs["model"] == "fixture-local"
    assert record.call_args.kwargs["tokens_out"] == 0
