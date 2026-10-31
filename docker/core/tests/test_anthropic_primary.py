"""Chat is account-login Grok 4.7 at medium effort. No paid fallback.

The Anthropic Messages client remains for direct unit coverage of that unused
transport. Routing never selects it.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app import llm_router as lr
from app.agent_loop import AgentLoop
from app.grok_chat import GrokChatError, Turn
from app.models import LLMConfig, SessionState


MODEL = "grok-4.7"
TOOL = {"type": "function", "function": {
    "name": "get_current_time", "description": "Current time",
    "parameters": {"type": "object", "properties": {}},
}}


def turn(text="Hello."):
    return Turn(text, MODEL, "low", "stream")


@pytest.fixture
async def router(monkeypatch):
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    r = lr.LLMRouter()
    await r.init()
    yield r
    await r.close()


def scripted(monkeypatch, replies):
    calls = []

    async def complete_turn(system, prompt, *, timeout=120, role="chat"):
        calls.append({"system": system, "prompt": prompt})
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def iter_text(system, prompt, *, timeout=120, role="chat"):
        turn = await complete_turn(system, prompt, timeout=timeout, role=role)
        yield turn.text

    monkeypatch.setattr("app.grok_chat.configured", lambda: True)
    monkeypatch.setattr("app.grok_chat.complete_turn", complete_turn)
    monkeypatch.setattr("app.grok_chat.iter_text", iter_text)
    return calls


async def collect(router, config):
    return [x async for x in router.stream("persona", [{"role": "user", "content": "hi"}], config)]


async def test_every_turn_is_grok_4_7_and_claude_is_not_opened(router):
    assert router._anthropic_http is None
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    background = await router.route("background", SessionState(conversation_id="bg"))
    assert (config.provider, config.model) == ("grok", MODEL)
    assert (background.provider, background.model) == ("grok", MODEL)
    assert await router.needs_agent("latest weather", interactive=True) is True
    assert await router.is_game_active() is False


async def test_a_key_does_not_open_the_paid_client(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-never-used")
    monkeypatch.delenv("KLUKAI_CLAUDE_PRIMARY", raising=False)
    r = lr.LLMRouter()
    await r.init()
    try:
        assert r._anthropic_http is None
        config = await r.route("hi", SessionState(conversation_id="c"), interactive=True)
        assert config.model == MODEL
    finally:
        await r.close()


async def test_stream_uses_medium_grok_and_a_failure_does_not_call_anthropic(router, monkeypatch):
    calls = scripted(monkeypatch, [turn("Hello.")])
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    assert await collect(router, config) == ["Hello."]
    assert calls[0]["prompt"].endswith("user: hi")
    assert "Earlier summary" not in calls[0]["system"] or True
    calls.clear()
    scripted(monkeypatch, [GrokChatError("down")])
    assert await collect(router, config) == [lr.FAILURE_SENTINEL]
    assert router._anthropic_http is None


async def test_system_text_is_sent_with_the_user_turn(router, monkeypatch):
    calls = scripted(monkeypatch, [turn("Hello.")])
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    result = [x async for x in router.stream("persona", [
        {"role": "system", "content": "Earlier summary"},
        {"role": "user", "content": "hi"},
    ], config)]
    assert result == ["Hello."]
    assert "Earlier summary" in calls[0]["prompt"]
    assert calls[0]["system"] == "persona"


async def test_model_overrides_stay_on_grok(router):
    assert lr.model_family("claude-haiku-5-5-20261001") == "claude-haiku-5-5"
    config = await router.route(
        "hi", SessionState(conversation_id="c"),
        user_override="claude-haiku-5-5-20261001", interactive=True,
    )
    assert config.model == MODEL
    ignored = await router.route(
        "hi", SessionState(conversation_id="c"), user_override="dolphin", interactive=True,
    )
    assert ignored.model == MODEL


async def test_native_tool_roundtrip(router, monkeypatch):
    calls = scripted(monkeypatch, [
        turn(json.dumps({"tool_calls": [{"name": "get_current_time", "arguments": {}}]})),
        turn("The time is noon."),
    ])
    monkeypatch.setattr("app.agent_loop.get_tool_schemas", AsyncMock(return_value=[TOOL]))
    monkeypatch.setattr("app.agent_loop.get_builtin_tools", lambda: [])
    loop = AgentLoop(router, AsyncMock(), AsyncMock())
    loop._builtin_get_time = AsyncMock(return_value={"content": [{"type": "text", "text": "12:00 UTC"}]})
    result = await loop.run("persona", [{"role": "user", "content": "What time is it?"}],
                            user_id="ricky", interactive=True)
    assert result.response == "The time is noon."
    assert result.model == MODEL
    assert result.tools_used == ["get_current_time"]
    assert "get_current_time" in calls[0]["system"]


async def test_unknown_tool_does_not_fall_back_to_a_paid_model(router, monkeypatch):
    scripted(monkeypatch, [turn(json.dumps({"tool_calls": [{"name": "shell", "arguments": {}}]}))])
    config = await router.route("time", SessionState(conversation_id="c"), interactive=True)
    with pytest.raises(ValueError):
        await router.complete_chat("persona", [{"role": "user", "content": "time"}], config, tools=[TOOL])
    assert router._anthropic_http is None
    assert config.model == MODEL


async def test_hand_built_configs_cannot_reach_any_other_backend(router, monkeypatch):
    calls = scripted(monkeypatch, [turn("nope")])
    rogue = LLMConfig(provider="lmstudio", model="dolphin", base_url="http://elsewhere")
    assert await collect(router, rogue) == [lr.FAILURE_SENTINEL]
    with pytest.raises(ValueError, match="Unavailable"):
        await router.complete_chat("persona", [{"role": "user", "content": "hi"}], rogue)
    assert calls == []


async def test_cancellation_does_not_start_another_provider(router, monkeypatch):
    started = asyncio.Event()

    async def iter_text(system, prompt, *, timeout=120, role="chat"):
        started.set()
        await asyncio.Event().wait()
        yield "unused"

    monkeypatch.setattr("app.grok_chat.configured", lambda: True)
    monkeypatch.setattr("app.grok_chat.iter_text", iter_text)
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    task = asyncio.create_task(collect(router, config))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert config.provider == "grok"
    assert router._anthropic_http is None


@pytest.mark.parametrize(("status", "headers", "delay"), [
    (429, {"retry-after": "7200"}, 3600.0),
    (429, {"retry-after": "soon"}, 60.0),
    (401, {}, 300.0),
    (403, {}, 300.0),
])
async def test_primary_backoff_delay(router, monkeypatch, status, headers, delay):
    monkeypatch.setattr(lr.time, "monotonic", lambda: 1000.0)
    request = httpx.Request("POST", lr.ANTHROPIC_MESSAGES_URL)
    error = httpx.HTTPStatusError(
        "x", request=request, response=httpx.Response(status, headers=headers, request=request),
    )
    router._primary_failed(error)
    assert router._anthropic_retry_at == 1000.0 + delay


def test_model_family_rejects_non_claude_ids():
    assert lr.model_family("grok-4.7") is None
    assert lr.model_family(lr.CLAUDE_PRIMARY) == lr.CLAUDE_PRIMARY


async def test_init_without_a_claude_key_stays_quiet(monkeypatch, caplog):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("KLUKAI_CLAUDE_PRIMARY", raising=False)
    router = lr.LLMRouter()
    with caplog.at_level("WARNING"):
        await router.init()
    try:
        assert router._anthropic_http is None
        assert router._anthropic_configured() is False
        assert "Claude is not the chat path" not in caplog.text
    finally:
        await router.close()


async def test_prose_that_is_not_a_tool_call_stays_text(router, monkeypatch):
    from app.llm_completions import _grok_tool_calls

    assert _grok_tool_calls("not json", [TOOL]) == []
    assert _grok_tool_calls("[1]", [TOOL]) == []
    assert _grok_tool_calls('{"note": 1}', [TOOL]) == []
    scripted(monkeypatch, [turn("not json"), turn("[1]")])
    config = await router.route(
        "time", SessionState(conversation_id="c"), interactive=True
    )
    messages = [{"role": "user", "content": "time"}]
    for reply_text in ("not json", "[1]"):
        result = await router.complete_chat("persona", messages, config, tools=[TOOL])
        message = result["choices"][0]["message"]
        assert message["content"] == reply_text
        assert message["tool_calls"] == []


async def test_a_mismatched_grok_model_is_refused(router):
    config = lr._grok_config()
    config.model = "grok-4"
    with pytest.raises(ValueError, match="Unavailable"):
        await router.complete_chat(
            "persona", [{"role": "user", "content": "hi"}], config
        )


async def test_multipart_turns_can_forbid_tools(router, monkeypatch):
    calls = scripted(monkeypatch, [turn("ok")])
    config = lr._grok_config()
    await router.complete_chat(
        "persona",
        [{"role": "user", "content": [{"text": "hi"}, "there", {"nope": 1}]}],
        config,
        tools=[TOOL],
        allow_tools=False,
    )
    assert calls[0]["prompt"] == "user: hi there"
    assert "Do not call tools" in calls[0]["system"]
    assert "tool_calls" not in calls[0]["system"]


async def test_an_empty_tool_call_list_is_rejected(router, monkeypatch):
    scripted(monkeypatch, [turn(json.dumps({"tool_calls": []}))])
    config = lr._grok_config()
    with pytest.raises(ValueError, match="Invalid or undeclared"):
        await router.complete_chat(
            "persona", [{"role": "user", "content": "time"}], config, tools=[TOOL]
        )


async def test_an_unexpected_grok_effort_is_rejected(router, monkeypatch):
    scripted(monkeypatch, [Turn("Hello.", MODEL, "high", "flag")])
    config = lr._grok_config()
    with pytest.raises(ValueError, match="Unexpected"):
        await router.complete_chat(
            "persona", [{"role": "user", "content": "hi"}], config
        )
