"""Real HTTP framing for native completion limits, EOF, and cancellation."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import Mock

import httpx
import pytest

from app import llm_router as routing
from app.models import LLMConfig

MESSAGES = [{"role": "user", "content": "hello"}]
SONNET = routing.CLAUDE_PRIMARY


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
    config = LLMConfig(provider="anthropic", model=SONNET)
    with pytest.raises(ValueError, match="model is unavailable"):
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
    config = LLMConfig(provider="anthropic", model=SONNET)
    with pytest.raises(ValueError, match="Missing response model"):
        await router._complete_anthropic("persona", MESSAGES, config)
    assert config.model == SONNET and len(requests) == 1


async def test_native_body_limit_stops_before_exposing_text(connect, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json=native_reply(content=[{"type": "text", "text": "private" * 100}])
        )

    router = connect(handler)
    monkeypatch.setattr(routing, "_ANTHROPIC_MAX_RESPONSE_BYTES", 64)
    config = LLMConfig(provider="anthropic", model=SONNET)
    with pytest.raises(ValueError, match="exceeds size limit"):
        await router._complete_anthropic("persona", MESSAGES, config)
    assert config.model == SONNET and len(requests) == 1
    assert router._anthropic_gate._value == 2


async def test_broken_usage_metadata_does_not_discard_valid_public_answer(connect):
    router = connect(
        lambda request: httpx.Response(
            200, json=native_reply(usage={"input_tokens": "not an integer"})
        )
    )
    config = LLMConfig(provider="anthropic", model=SONNET)
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
    fallback, failed = Mock(), Mock()
    monkeypatch.setattr(router, "_select_secondary", fallback)
    monkeypatch.setattr(router, "_primary_failed", failed)
    task = asyncio.create_task(
        router.complete_chat(
            "persona", MESSAGES, LLMConfig(provider="anthropic", model=SONNET)
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set() and router._anthropic_gate._value == 2
        assert len(requests) == 1
        fallback.assert_not_called()
        failed.assert_not_called()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


TOOL = {
    "type": "function",
    "function": {
        "name": "get_current_time",
        "description": "Current time",
        "parameters": {"type": "object", "properties": {}},
    },
}


async def test_unused_sonnet_client_still_falls_back_to_haiku(connect):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["model"])
        if len(calls) == 1:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json=native_reply())

    router = connect(handler)
    config = LLMConfig(
        provider="anthropic", model=SONNET, max_tokens=128, read_timeout=30
    )
    result = await router.complete_chat("persona", MESSAGES, config)
    assert calls == [SONNET, routing.CLAUDE_SECONDARY]
    assert result["choices"][0]["message"]["content"] == "public reply"


async def test_a_failed_haiku_does_not_switch_to_another_claude_model(connect):
    def handler(request):
        raise httpx.ConnectError("down")

    router = connect(handler)
    config = LLMConfig(provider="anthropic", model=routing.CLAUDE_SECONDARY)
    with pytest.raises(httpx.ConnectError):
        await router._complete_with_fallback(
            "persona", MESSAGES, config, grok_fallback=False
        )
    assert config.model == routing.CLAUDE_SECONDARY


async def test_backing_off_primary_is_refused(connect, monkeypatch):
    monkeypatch.setattr(routing.time, "monotonic", lambda: 0.0)
    router = connect(lambda request: httpx.Response(200, json=native_reply()))
    router._anthropic_retry_at = 10.0
    config = LLMConfig(provider="anthropic", model=SONNET)
    with pytest.raises(ValueError, match="backing off"):
        await router._complete_anthropic("persona", MESSAGES, config)


async def test_native_tools_can_be_offered_or_suppressed(connect):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=native_reply())

    router = connect(handler)
    config = LLMConfig(provider="anthropic", model=SONNET)
    await router._complete_anthropic("persona", MESSAGES, config, tools=[TOOL])
    config.model = SONNET
    await router._complete_anthropic(
        "persona", MESSAGES, config, tools=[TOOL], allow_tools=False
    )
    assert seen[0]["tools"][0]["name"] == "get_current_time"
    assert "tool_choice" not in seen[0]
    assert seen[1]["tool_choice"] == {"type": "none"}
