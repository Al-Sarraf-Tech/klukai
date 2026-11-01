"""Chat is Claude Haiku 5.5 at xhigh effort when Claude is switched on.

Owner decision 2026-10-31: "set chat model to haiku xhigh". The switch is
KLUKAI_CLAUDE_PRIMARY=true with ANTHROPIC_API_KEY. A Haiku failure before any
text falls back to the Grok chat path (xAI API, then the account CLI).
No live inference: the Anthropic API is a mock transport.
"""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app import llm_router as lr
from app.agent_loop import AgentLoop
from app.grok_chat import Turn
from app.models import SessionState


HAIKU = "claude-haiku-5-5"
HAIKU_DATED = "claude-haiku-5-5-20261001"
TOOL = {"type": "function", "function": {
    "name": "get_current_time", "description": "Current time",
    "parameters": {"type": "object", "properties": {}},
}}


def reply(content=None, stop_reason="end_turn", model=HAIKU_DATED):
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": model,
            "content": content if content is not None else [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": "Hello, Commander."},
            ],
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


def grok(monkeypatch, replies):
    """Script the Grok fallback (the CLI path; the xAI API is off in tests)."""
    calls = []

    async def complete_turn(system, prompt, *, timeout=120, role="chat"):
        calls.append(prompt)
        return Turn(replies.pop(0), "grok-4.7", "low", "stream")

    async def iter_text(system, prompt, *, timeout=120, role="chat"):
        turn = await complete_turn(system, prompt, timeout=timeout, role=role)
        yield turn.text

    monkeypatch.setenv("KLUKAI_CHAT_BACKEND", "cli")
    monkeypatch.setattr("app.grok_chat.configured", lambda: True)
    monkeypatch.setattr("app.grok_chat.complete_turn", complete_turn)
    monkeypatch.setattr("app.grok_chat.iter_text", iter_text)
    return calls


async def collect(router, config):
    return [x async for x in router.stream("persona", [{"role": "user", "content": "hi"}], config)]


async def test_every_turn_routes_to_haiku(router):
    assert router._anthropic_configured() is True
    chat = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    background = await router.route("bg", SessionState(conversation_id="bg"))
    assert (chat.provider, chat.model) == ("anthropic", HAIKU)
    assert (background.provider, background.model) == ("anthropic", HAIKU)
    assert chat.max_tokens == lr.CHAT_MAX_TOKENS


async def test_model_overrides_stay_on_haiku(router):
    for override in ("claude-sonnet-5-5", "grok-4.7", "dolphin", HAIKU_DATED):
        config = await router.route(
            "hi", SessionState(conversation_id="c"),
            user_override=override, interactive=True,
        )
        assert config.model == HAIKU


async def test_the_request_is_haiku_at_xhigh_with_adaptive_thinking(router):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply())

    await transport(router, handler)
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    assert await collect(router, config) == ["Hello, Commander."]
    body = seen[0]
    assert body["model"] == HAIKU
    assert body["output_config"] == {"effort": "xhigh"}
    assert body["thinking"] == {"type": "adaptive"}
    assert body["max_tokens"] == lr.CHAT_MAX_TOKENS
    for sampling in ("temperature", "top_p", "top_k"):
        assert sampling not in body
    assert config.model == HAIKU_DATED


@pytest.mark.parametrize("failure", [
    httpx.Response(529, json={"type": "error"}),
    httpx.Response(200, json=reply(stop_reason="max_tokens")),
    httpx.Response(200, json=reply(content=[])),
])
async def test_a_haiku_failure_falls_back_to_grok(router, monkeypatch, failure):
    calls = grok(monkeypatch, ["Grok here."])
    await transport(router, lambda request: failure)
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    assert await collect(router, config) == ["Grok here."]
    assert (config.provider, config.model) == ("grok", lr.GROK_MODEL)
    assert calls and calls[0].endswith("user: hi")


async def test_a_haiku_connection_error_falls_back_to_grok(router, monkeypatch):
    grok(monkeypatch, ["Grok here."])

    def handler(request):
        raise httpx.ConnectError("down", request=request)

    await transport(router, handler)
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    assert await collect(router, config) == ["Grok here."]


async def test_the_tool_loop_falls_back_to_grok(router, monkeypatch):
    grok(monkeypatch, ["The time is noon."])
    await transport(router, lambda request: httpx.Response(500, json={"type": "error"}))
    config = await router.route("time", SessionState(conversation_id="c"), interactive=True)
    result = await router.complete_chat(
        "persona", [{"role": "user", "content": "time"}], config, tools=[TOOL],
    )
    assert result["choices"][0]["message"]["content"] == "The time is noon."
    assert config.provider == "grok"


async def test_native_tools_run_on_haiku(router, monkeypatch):
    replies = [
        reply(content=[{"type": "tool_use", "id": "t1", "name": "get_current_time", "input": {}}],
              stop_reason="tool_use"),
        reply(content=[{"type": "text", "text": "The time is noon."}]),
    ]
    await transport(router, lambda request: httpx.Response(200, json=replies.pop(0)))
    monkeypatch.setattr("app.agent_loop.get_tool_schemas", AsyncMock(return_value=[TOOL]))
    monkeypatch.setattr("app.agent_loop.get_builtin_tools", lambda: [])
    loop = AgentLoop(router, AsyncMock(), AsyncMock())
    loop._builtin_get_time = AsyncMock(
        return_value={"content": [{"type": "text", "text": "12:00 UTC"}]})
    result = await loop.run("persona", [{"role": "user", "content": "What time is it?"}],
                            user_id="ricky", interactive=True)
    assert result.response == "The time is noon."
    assert result.tools_used == ["get_current_time"]


async def test_the_flag_without_a_key_stays_on_grok(monkeypatch, caplog):
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = lr.LLMRouter()
    with caplog.at_level("WARNING"):
        await r.init()
    try:
        assert r._anthropic_http is None
        config = await r.route("hi", SessionState(conversation_id="c"), interactive=True)
        assert config.provider == "grok"
        assert "without ANTHROPIC_API_KEY" in caplog.text
    finally:
        await r.close()


def test_chat_provider_reports_the_selected_model(monkeypatch):
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert lr.chat_provider() == ("anthropic", HAIKU)
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "false")
    assert lr.chat_provider() == ("grok", lr.GROK_MODEL)
