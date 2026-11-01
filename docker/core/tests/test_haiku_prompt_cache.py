"""Her persona is a cached prompt block, so replies stay fast at xhigh.

Owner decision 2026-11-01: "Prompt cache then". Measured on her real persona at
xhigh, first text took 3.5-3.85 s uncached and about 2.5 s cached.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app import llm_router as lr
from app.anthropic_messages import system_blocks
from app.models import SessionState
from app.personality.system_prompt import PromptText

HAIKU = "claude-haiku-5-5"
SECRET = "the Commander's private words"
CACHED = {"type": "ephemeral", "ttl": "1h"}


def reply(**usage):
    return {
        "type": "message",
        "role": "assistant",
        "model": HAIKU,
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": SECRET}],
        "usage": {"input_tokens": 500, "output_tokens": 40, **usage},
    }


@pytest.fixture
async def router(monkeypatch):
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    r = lr.LLMRouter()
    await r.init()
    yield r
    await r.close()


async def capture(router, body=None):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=body or reply())

    await router._anthropic_http.aclose()
    router._anthropic_http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"x-api-key": "test-anthropic-key", "anthropic-version": "2023-06-01"},
    )
    return seen


async def route(router):
    return await router.route("hi", SessionState(conversation_id="c"), interactive=True)


def test_the_persona_is_cached_and_the_turn_state_is_not():
    prompt = PromptText("PERSONA\n\nTURN STATE", "PERSONA")
    assert system_blocks(prompt, f"{prompt}\n\nEarlier summary") == [
        {"type": "text", "text": "PERSONA", "cache_control": CACHED},
        {"type": "text", "text": "TURN STATE\n\nEarlier summary"},
    ]


def test_a_persona_alone_is_one_cached_block():
    prompt = PromptText("PERSONA", "PERSONA")
    assert system_blocks(prompt, str(prompt)) == [
        {"type": "text", "text": "PERSONA", "cache_control": CACHED}
    ]


@pytest.mark.parametrize(
    "prompt",
    ["plain text", PromptText("no shared prefix", ""), PromptText("other", "PERSONA")],
)
def test_a_prompt_without_a_stable_prefix_stays_a_plain_string(prompt):
    assert system_blocks(prompt, str(prompt)) == str(prompt)


async def test_a_chat_reply_sends_the_cached_persona(router):
    seen = await capture(router)
    prompt = PromptText("PERSONA\n\nTURN STATE", "PERSONA")
    pieces = [
        piece
        async for piece in router.stream(
            prompt, [{"role": "user", "content": "hi"}], await route(router)
        )
    ]
    assert pieces == [SECRET]
    assert seen[0]["system"][0] == {
        "type": "text",
        "text": "PERSONA",
        "cache_control": CACHED,
    }
    assert seen[0]["system"][1] == {"type": "text", "text": "TURN STATE"}
    assert seen[0]["output_config"] == {"effort": "xhigh"}


async def test_the_tool_loop_sends_the_cached_persona(router):
    seen = await capture(router)
    prompt = PromptText("PERSONA\n\nTURN STATE", "PERSONA")
    tool = {
        "type": "function",
        "function": {"name": "t", "description": "d", "parameters": {"type": "object"}},
    }
    await router.complete_chat(
        prompt, [{"role": "user", "content": "t"}], await route(router), tools=[tool]
    )
    assert seen[0]["system"][0]["cache_control"] == CACHED


async def test_a_plain_prompt_is_still_sent_as_a_string(router):
    seen = await capture(router)
    await router.complete_chat(
        "persona", [{"role": "user", "content": "t"}], await route(router)
    )
    assert seen[0]["system"] == "persona"


async def test_cache_use_is_logged_without_content(router, caplog):
    await capture(
        router, reply(cache_read_input_tokens=7200, cache_creation_input_tokens=0)
    )
    with caplog.at_level(logging.INFO, logger="app.llm_completions"):
        await router.complete_chat(
            "persona", [{"role": "user", "content": SECRET}], await route(router)
        )
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Claude chat:")
    ]
    assert len(lines) == 1
    assert f"model={HAIKU}" in lines[0] and "total_ms=" in lines[0]
    assert "input=500 cache_read=7200 cache_write=0 output=40" in lines[0]
    assert SECRET not in caplog.text
