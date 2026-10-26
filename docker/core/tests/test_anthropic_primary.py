"""Cloud-only Claude routing (Sonnet chat, Haiku otherwise) and native tools.

No live inference: the Anthropic API is a mock transport."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app import llm_router as lr
from app.agent_loop import AgentLoop
from app.models import LLMConfig, SessionState


MODEL = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"
HAIKU_DATED = "claude-haiku-5-5-20261001"
TOOL = {"type": "function", "function": {
    "name": "get_current_time", "description": "Current time",
    "parameters": {"type": "object", "properties": {}},
}}


def reply(content=None, stop_reason="end_turn", model=MODEL):
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": model,
            "content": content if content is not None else [{"type": "text", "text": "Hello."}],
            "stop_reason": stop_reason, "usage": {"input_tokens": 10, "output_tokens": 8}}


@pytest.fixture
async def router(monkeypatch):
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    r = lr.LLMRouter()
    await r.init()
    yield r
    await r.close()


async def transport(router, handler):
    await router._anthropic_http.aclose()
    router._anthropic_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"x-api-key": "test-anthropic-key", "anthropic-version": "2023-06-01"},
    )


async def collect(router, config):
    return [x async for x in router.stream("persona", [{"role": "user", "content": "hi"}], config)]


async def test_interactive_uses_sonnet_and_everything_else_haiku(router):
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    assert (config.provider, config.model) == ("anthropic", MODEL)
    background = await router.route("background", SessionState(conversation_id="bg"))
    assert (background.provider, background.model) == ("anthropic", HAIKU)
    assert await router.needs_agent("latest weather", interactive=True) is True
    assert await router.needs_agent("latest weather") is True
    assert await router.is_game_active() is False

async def test_key_alone_does_not_enable_billable_primary(monkeypatch):
    monkeypatch.delenv("KLUKAI_CLAUDE_PRIMARY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-never-used")
    r = lr.LLMRouter()
    await r.init()
    try:
        assert r._anthropic_http is None
        with pytest.raises(RuntimeError, match="No cloud LLM"):
            await r.route("hi", SessionState(conversation_id="c"), interactive=True)
        assert await r.needs_agent("latest weather", interactive=True) is False
    finally:
        await r.close()


async def test_flag_without_key_leaves_chat_unavailable(monkeypatch):
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = lr.LLMRouter()
    await r.init()
    try:
        assert r._anthropic_http is None
        with pytest.raises(RuntimeError):
            await r.route("hi", SessionState(conversation_id="c"))
    finally:
        await r.close()

async def test_api_body_medium_adaptive_and_only_final_text_is_exposed(router):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=reply([
            {"type": "thinking", "thinking": "private reasoning", "signature": "sig"},
            {"type": "text", "text": "Hello."},
        ]))
    await transport(router, handler)
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    result = [x async for x in router.stream("persona", [
        {"role": "system", "content": "Earlier summary"},
        {"role": "user", "content": "hi"},
    ], config)]
    assert result == ["Hello."]
    request = calls[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "test-anthropic-key"
    assert "authorization" not in request.headers
    body = json.loads(request.content)
    assert body["model"] == MODEL
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "medium"}
    assert "temperature" not in body
    assert "Earlier summary" in body["system"]
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


async def test_quota_failure_falls_back_to_haiku_and_records_actual_model(router):
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["model"] == MODEL:
            return httpx.Response(429, headers={"retry-after": "120"}, json={"error": {"type": "rate_limit_error"}})
        return httpx.Response(200, json=reply([{"type": "text", "text": "Haiku answer."}], model=HAIKU_DATED))
    await transport(router, handler)
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    assert await collect(router, config) == ["Haiku answer."]
    assert config.model == HAIKU_DATED
    assert [c["model"] for c in calls] == [MODEL, HAIKU]
    assert calls[1]["output_config"] == {"effort": "high"}
    # Sonnet is backing off: the next interactive turn goes straight to Haiku.
    next_config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    assert next_config.model == HAIKU
    assert await collect(router, next_config) == ["Haiku answer."]
    assert [c["model"] for c in calls] == [MODEL, HAIKU, HAIKU]


async def test_haiku_failure_does_not_retry_and_returns_only_the_sentinel(router):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(500, json={"error": {"type": "api_error"}})
    await transport(router, handler)
    config = await router.route("bg", SessionState(conversation_id="c"))
    assert await collect(router, config) == [lr.FAILURE_SENTINEL]
    assert calls == [HAIKU]
    assert router._anthropic_ready() is True  # a Haiku failure never parks Sonnet


@pytest.mark.parametrize(("status", "headers", "delay"), [
    (429, {"retry-after": "7200"}, 3600.0),
    (429, {"retry-after": "soon"}, 60.0),
    (401, {}, 300.0),
    (403, {}, 300.0),
])
async def test_primary_backoff_delay(router, monkeypatch, status, headers, delay):
    monkeypatch.setattr(lr.time, "monotonic", lambda: 1000.0)
    request = httpx.Request("POST", lr.ANTHROPIC_MESSAGES_URL)
    error = httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, headers=headers, request=request))
    router._primary_failed(error)
    assert router._anthropic_retry_at == 1000.0 + delay


async def test_timeout_backs_off_for_a_minute(router, monkeypatch):
    monkeypatch.setattr(lr.time, "monotonic", lambda: 50.0)
    router._primary_failed(TimeoutError())
    assert router._anthropic_retry_at == 110.0


async def test_dated_model_ids_keep_their_family(router):
    assert lr.model_family(HAIKU_DATED) == HAIKU
    assert lr.model_family("claude-sonnet-5-5-20261001") == MODEL
    assert lr.model_family("claude-opus-5-5") is None
    config = await router.route("hi", SessionState(conversation_id="c"), user_override=HAIKU_DATED, interactive=True)
    assert config.model == HAIKU
    ignored = await router.route("hi", SessionState(conversation_id="c"), user_override="dolphin", interactive=True)
    assert ignored.model == MODEL

async def test_primary_and_secondary_failure_emit_only_failure_sentinel(router):
    await transport(router, lambda request: httpx.Response(401, json={"error": {"type": "authentication_error"}}))
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    assert await collect(router, config) == [lr.FAILURE_SENTINEL]


async def test_cancellation_does_not_start_secondary_inference(router):
    started = asyncio.Event()
    async def handler(request):
        started.set()
        await asyncio.Event().wait()
    await transport(router, handler)
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    task = asyncio.create_task(collect(router, config))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert config.provider == "anthropic"


async def test_native_tool_roundtrip_preserves_thinking_and_never_exposes_it(router, monkeypatch):
    calls = []
    thinking = {"type": "thinking", "thinking": "private", "signature": "signed"}
    tool_use = {"type": "tool_use", "id": "toolu_1", "name": "get_current_time", "input": {}}
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=reply(
            [thinking, tool_use] if len(calls) == 1 else [{"type": "text", "text": "The time is noon."}],
            "tool_use" if len(calls) == 1 else "end_turn"))
    await transport(router, handler)
    monkeypatch.setattr("app.agent_loop.get_tool_schemas", AsyncMock(return_value=[TOOL]))
    monkeypatch.setattr("app.agent_loop.get_builtin_tools", lambda: [])
    loop = AgentLoop(router, AsyncMock(), AsyncMock())
    loop._builtin_get_time = AsyncMock(return_value={"content": [{"type": "text", "text": "12:00 UTC"}]})
    result = await loop.run("persona", [{"role": "user", "content": "What time is it?"}],
                            user_id="ricky", interactive=True)
    assert result.response == "The time is noon."
    assert result.model == MODEL
    assert result.tools_used == ["get_current_time"]
    assert calls[0]["tools"] == [{"name": "get_current_time", "description": "Current time",
                                 "input_schema": {"type": "object", "properties": {}}}]
    assert calls[1]["messages"][1] == {"role": "assistant", "content": [thinking, tool_use]}
    assert calls[1]["messages"][2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "12:00 UTC"}]}
    assert all("private" not in step.content for step in result.steps)


async def test_unknown_tool_selection_fails_over_to_haiku_without_executing_it(router):
    def handler(request):
        if json.loads(request.content)["model"] == MODEL:
            return httpx.Response(200, json=reply([
                {"type": "tool_use", "id": "bad", "name": "shell", "input": {"cmd": "bad"}}
            ], "tool_use"))
        return httpx.Response(200, json=reply([{"type": "text", "text": "Haiku."}], model=HAIKU))
    await transport(router, handler)
    config = await router.route("time", SessionState(conversation_id="c"), interactive=True)
    result = await router.complete_chat("persona", [{"role": "user", "content": "time"}], config, tools=[TOOL])
    assert result["choices"][0]["message"]["content"] == "Haiku."
    assert (config.provider, config.model) == ("anthropic", HAIKU)


async def test_haiku_tool_failure_raises_to_the_loop(router):
    await transport(router, lambda request: httpx.Response(200, json=reply([
        {"type": "tool_use", "id": "bad", "name": "shell", "input": {}}
    ], "tool_use", model=HAIKU)))
    config = await router.route("bg", SessionState(conversation_id="c"))
    with pytest.raises(ValueError):
        await router.complete_chat("persona", [{"role": "user", "content": "time"}], config, tools=[TOOL])

async def test_forced_synthesis_keeps_tool_definitions_but_disables_more_calls(router, monkeypatch):
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(200, json=reply([
                {"type": "tool_use", "id": "toolu_1", "name": "get_current_time", "input": {}}
            ], "tool_use"))
        if not body.get("tools") or body.get("tool_choice") != {"type": "none"}:
            return httpx.Response(400, json={"error": {"type": "invalid_request_error"}})
        return httpx.Response(200, json=reply())
    await transport(router, handler)
    monkeypatch.setattr("app.agent_loop.MAX_ITERATIONS", 1)
    monkeypatch.setattr("app.agent_loop.get_tool_schemas", AsyncMock(return_value=[TOOL]))
    monkeypatch.setattr("app.agent_loop.get_builtin_tools", lambda: [])
    loop = AgentLoop(router, AsyncMock(), AsyncMock())
    result = await loop.run("persona", [{"role": "user", "content": "time"}], interactive=True)
    assert result.response == "Hello."
    assert len(requests) == 2


async def test_hand_built_configs_cannot_reach_any_other_backend(router):
    calls = []
    await transport(router, lambda request: calls.append(request) or httpx.Response(200, json=reply()))
    rogue = LLMConfig(provider="lmstudio", model="dolphin", base_url="http://elsewhere")
    assert await collect(router, rogue) == [lr.FAILURE_SENTINEL]
    with pytest.raises(ValueError, match="Unavailable"):
        await router.complete_chat("persona", [{"role": "user", "content": "hi"}], rogue)
    unknown = LLMConfig(provider="anthropic", model="claude-opus-5-5", base_url=lr.ANTHROPIC_MESSAGES_URL)
    with pytest.raises(ValueError):
        await router.complete_chat("persona", [{"role": "user", "content": "hi"}], unknown)
    assert calls == []


async def test_backing_off_primary_is_not_called(router, monkeypatch):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(200, json=reply(model=HAIKU))
    await transport(router, handler)
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    router._anthropic_retry_at = float("inf")
    assert await collect(router, config) == ["Hello."]
    assert calls == [HAIKU]