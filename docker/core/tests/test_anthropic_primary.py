"""Interactive Anthropic routing and native tools, with no live inference."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app import llm_router as lr
from app.agent_loop import AgentLoop
from app.models import LLMConfig, SessionState


MODEL = "claude-sonnet-5-5"
TOOL = {"type": "function", "function": {
    "name": "get_current_time", "description": "Current time",
    "parameters": {"type": "object", "properties": {}},
}}


def reply(content=None, stop_reason="end_turn"):
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": MODEL,
            "content": content if content is not None else [{"type": "text", "text": "Hello."}],
            "stop_reason": stop_reason, "usage": {"input_tokens": 10, "output_tokens": 8}}


@pytest.fixture
async def router(monkeypatch):
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("LM_STUDIO_TOKEN", "test-local-key")
    monkeypatch.setattr(lr.LLMRouter, "_check_lmstudio", AsyncMock(return_value=False))
    r = lr.LLMRouter()
    await r.init()
    r._lmstudio_available = False
    r._lmstudio_last_check = float("inf")
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


async def test_interactive_primary_available_with_dominus_offline_background_stays_local(router):
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    assert config.provider == "anthropic"
    assert config.model == MODEL
    with pytest.raises(RuntimeError, match="No local"):
        await router.route("background", SessionState(conversation_id="bg"))
    assert await router.needs_agent("latest weather", interactive=True) is True
    assert await router.needs_agent("latest weather") is False


async def test_key_alone_does_not_enable_billable_primary(monkeypatch):
    monkeypatch.delenv("KLUKAI_CLAUDE_PRIMARY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-never-used")
    monkeypatch.setattr(lr.LLMRouter, "_check_lmstudio", AsyncMock(return_value=False))
    r = lr.LLMRouter()
    await r.init()
    try:
        assert r._anthropic_http is None
        with pytest.raises(RuntimeError):
            await r.route("hi", SessionState(conversation_id="c"), interactive=True)
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


async def test_quota_failure_falls_back_before_output_and_records_actual_model(router):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "120"}, json={"error": {"type": "rate_limit_error"}})
    await transport(router, handler)
    router._lmstudio_available = True
    router.is_game_active = AsyncMock(return_value=True)
    async def local(*args):
        yield "Dominus answer."
    router._stream_lmstudio = local
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    assert await collect(router, config) == ["Dominus answer."]
    assert config.provider == "lmstudio"
    assert config.model == lr.LOCAL_CASUAL_GAMING
    next_config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    assert next_config.provider == "lmstudio"
    assert len(calls) == 1


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


async def test_unknown_tool_selection_fails_over_without_executing_it(router):
    await transport(router, lambda request: httpx.Response(200, json=reply([
        {"type": "tool_use", "id": "bad", "name": "shell", "input": {"cmd": "bad"}}
    ], "tool_use")))
    router._lmstudio_available = True
    router.is_game_active = AsyncMock(return_value=False)
    router.complete_local = AsyncMock(return_value={"choices": [{"message": {"content": "Local."}}]})
    config = await router.route("time", SessionState(conversation_id="c"), interactive=True)
    result = await router.complete_chat("persona", [{"role": "user", "content": "time"}], config, tools=[TOOL])
    assert result["choices"][0]["message"]["content"] == "Local."
    assert config.provider == "lmstudio"


async def test_native_metadata_is_not_sent_to_dominus(router):
    seen = []
    await router._http.aclose()
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Local."}}]})
    router._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await router.complete_local("persona", [{"role": "assistant", "content": "hello",
        "_anthropic_content": [{"type": "thinking", "thinking": "private", "signature": "sig"}]}],
        LLMConfig(provider="lmstudio", model=lr.LOCAL_CASUAL, base_url="http://local"))
    assert seen[0]["messages"][1] == {"role": "assistant", "content": "hello"}


async def test_api_primary_does_not_warm_the_secondary_gpu(monkeypatch):
    from app import warmup
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("KLUKAI_DISABLE_WARMUP", raising=False)
    monkeypatch.setattr(warmup, "is_loaded", AsyncMock(side_effect=AssertionError("GPU should stay asleep")))
    assert await warmup.warm_chat_model(force=True) is False


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


async def test_complete_local_rejects_nonlocal_provider_without_a_request(router):
    await router._http.aclose()
    router._http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "unexpected"}}]})
    ))
    with pytest.raises(ValueError, match="local"):
        await router.complete_local("persona", [{"role": "user", "content": "hi"}],
            LLMConfig(provider="anthropic", model=MODEL, base_url=lr.ANTHROPIC_MESSAGES_URL))
