"""Haiku chat streams its text, fails over to Grok fast, and pays its way.

Owner, 2026-11-04: stream Haiku token by token; one or two bad turns move chat
to xAI Grok, then the CLI; every Anthropic charge counts against the $5 cap.
No live inference: the Messages API is a mock transport.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import anthropic_chat
from app import llm_router as lr
from app import xai_spend
from app.circuit_breakers import State, get_breaker
from app.grok_chat import Turn
from app.models import SessionState
from app.personality.system_prompt import PromptText
from tests.anthropic_sse import body as sse_body
from tests.anthropic_sse import events

HAIKU_DATED = "claude-haiku-5-5-20261001"
USAGE = {
    "input_tokens": 40,
    "cache_read_input_tokens": 7000,
    "cache_creation_input_tokens": 300,
    "output_tokens": 25,
}


def reply(*texts, stop_reason="end_turn"):
    return {
        "model": HAIKU_DATED,
        "content": [{"type": "thinking", "thinking": ""}]
        + [{"type": "text", "text": text} for text in texts],
        "stop_reason": stop_reason,
        "usage": dict(USAGE),
    }


@pytest.fixture
async def router(monkeypatch):
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    r = lr.LLMRouter()
    await r.init()
    yield r
    await r.close()


async def use(router, handler):
    await router._anthropic_http.aclose()
    router._anthropic_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def grok(monkeypatch, text="Grok here."):
    calls = []

    async def iter_text(system, prompt, *, timeout=120, role="chat"):
        calls.append(prompt)
        yield text

    async def complete_turn(system, prompt, *, timeout=120, role="chat"):
        calls.append(prompt)
        return Turn(text, "grok-4.7", "low", "stream")

    monkeypatch.setenv("KLUKAI_CHAT_BACKEND", "cli")
    monkeypatch.setattr("app.grok_chat.configured", lambda: True)
    monkeypatch.setattr("app.grok_chat.iter_text", iter_text)
    monkeypatch.setattr("app.grok_chat.complete_turn", complete_turn)
    return calls


async def turn(router, prompt="persona", messages=None):
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    pieces = [
        piece
        async for piece in router.stream(
            prompt, messages or [{"role": "user", "content": "hi"}], config
        )
    ]
    return pieces, config


def sse(items):
    return httpx.Response(
        200, text=sse_body(items), headers={"content-type": "text/event-stream"}
    )


async def test_text_streams_piece_by_piece_and_records_the_charge(router, caplog):
    caplog.set_level("INFO")
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return sse(events(reply("Hello, ", "Commander.")))

    await use(router, handler)
    pieces, config = await turn(router)
    assert pieces == ["Hello, ", "Commander."]
    assert config.model == HAIKU_DATED
    assert seen[0]["stream"] is True
    assert xai_spend.spent_today() == pytest.approx(xai_spend.anthropic_usd(USAGE))
    line = next(r.getMessage() for r in caplog.records if "Claude chat:" in r.getMessage())
    assert "ttft_ms=" in line and "ttft_ms=-" not in line
    assert "cache_read=7000" in line and "Commander" not in line


async def test_thinking_is_never_forwarded(router):
    items = events(reply("Visible."))
    await use(router, lambda request: sse(items))
    pieces, _config = await turn(router)
    assert pieces == ["Visible."]


async def test_a_spent_cap_sends_nothing_and_answers_with_grok(router, monkeypatch):
    monkeypatch.setenv("KLUKAI_API_DAILY_USD_CAP", "0")
    calls = grok(monkeypatch)
    sent = []
    await use(router, lambda request: sent.append(request) or sse(events(reply("x"))))
    pieces, config = await turn(router)
    assert pieces == ["Grok here."]
    assert sent == [] and calls
    assert config.provider == "grok"


async def test_two_bad_turns_open_the_breaker_and_the_third_skips_claude(
    router, monkeypatch
):
    grok(monkeypatch)
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(500)

    await use(router, handler)
    for _ in range(2):
        pieces, _config = await turn(router)
        assert pieces == ["Grok here."]
    assert get_breaker("anthropic").state is State.OPEN
    pieces, _config = await turn(router)
    assert pieces == ["Grok here."]
    assert len(sent) == 2
    assert xai_spend.spent_today() == 0


@pytest.mark.parametrize(("status", "retry_after"), [(401, None), (429, "120")])
async def test_a_key_problem_or_rate_limit_backs_claude_off(
    router, monkeypatch, status, retry_after
):
    grok(monkeypatch)
    sent = []
    headers = {"retry-after": retry_after} if retry_after else {}

    def handler(request):
        sent.append(request)
        return httpx.Response(status, headers=headers)

    await use(router, handler)
    await turn(router)
    assert anthropic_chat.backing_off()
    pieces, _config = await turn(router)
    assert pieces == ["Grok here."]
    assert len(sent) == 1


async def test_no_first_text_in_time_answers_with_grok(router, monkeypatch):
    monkeypatch.setenv("KLUKAI_CLAUDE_FIRST_TEXT_S", "0.2")
    grok(monkeypatch)
    start = events(reply("late"))[0]

    async def slow():
        yield f"data: {json.dumps(start)}\n\n".encode()
        await asyncio.sleep(5)
        yield b""

    await use(router, lambda request: httpx.Response(200, content=slow()))
    loop = asyncio.get_running_loop()
    began = loop.time()
    pieces, config = await turn(router)
    assert pieces == ["Grok here."]
    assert loop.time() - began < 2
    assert config.provider == "grok"
    # The request was sent and Claude had started: it is counted.
    assert xai_spend.spent_today() > 0


async def test_no_event_in_time_answers_with_grok(router, monkeypatch):
    monkeypatch.setenv("KLUKAI_CLAUDE_FIRST_EVENT_S", "0.2")
    grok(monkeypatch)

    async def silent():
        await asyncio.sleep(5)
        yield b""

    await use(router, lambda request: httpx.Response(200, content=silent()))
    pieces, _config = await turn(router)
    assert pieces == ["Grok here."]


async def test_a_stream_that_dies_after_text_ends_the_turn_and_is_charged(
    router, monkeypatch
):
    calls = grok(monkeypatch)
    items = events(reply("Half a "))[:6] + [{"type": "error", "error": {}}]
    await use(router, lambda request: sse(items))
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    got = []
    with pytest.raises(lr.LLMStreamFailed):
        async for piece in router.stream("persona", [{"role": "user", "content": "hi"}], config):
            got.append(piece)
    assert got == ["Half a "]
    assert calls == []
    assert xai_spend.spent_today() > 0


async def test_a_client_that_leaves_mid_reply_still_records_the_charge(router):
    await use(router, lambda request: sse(events(reply("One. ", "Two."))))
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    stream = router.stream("persona", [{"role": "user", "content": "hi"}], config)
    assert await anext(stream) == "One. "
    await stream.aclose()
    assert xai_spend.spent_today() > 0


@pytest.mark.parametrize(
    "items",
    [
        [{"type": "message_stop"}],
        events(reply())[:-2]
        + [{"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}}],
    ],
)
async def test_a_reply_without_text_answers_with_grok(router, monkeypatch, items):
    grok(monkeypatch)
    await use(router, lambda request: sse(items))
    pieces, _config = await turn(router)
    assert pieces == ["Grok here."]


async def test_a_refusal_without_text_is_said_plainly(router):
    items = events(reply(stop_reason="refusal"))
    await use(router, lambda request: sse(items))
    pieces, _config = await turn(router)
    assert pieces == ["I can't help with that request."]


async def test_malformed_and_oversized_streams_answer_with_grok(router, monkeypatch):
    grok(monkeypatch)
    await use(router, lambda request: httpx.Response(200, text="data: {nope\n\n"))
    assert (await turn(router))[0] == ["Grok here."]
    get_breaker("anthropic").force_close()
    monkeypatch.setattr(anthropic_chat, "_MAX_BODY", 10)
    await use(router, lambda request: sse(events(reply("long enough text"))))
    assert (await turn(router))[0] == ["Grok here."]


async def test_a_busy_gate_answers_with_grok(router, monkeypatch):
    grok(monkeypatch)
    monkeypatch.setattr(anthropic_chat, "GATE_WAIT_S", 0.05)
    await use(router, lambda request: sse(events(reply("x"))))
    for _ in range(2):
        await router._anthropic_gate.acquire()
    try:
        assert (await turn(router))[0] == ["Grok here."]
    finally:
        router._anthropic_gate.release()
        router._anthropic_gate.release()
    assert xai_spend.spent_today() == 0


async def test_a_connection_error_answers_with_grok(router, monkeypatch):
    grok(monkeypatch)

    def handler(request):
        raise httpx.ConnectError("down", request=request)

    await use(router, handler)
    assert (await turn(router))[0] == ["Grok here."]


async def test_history_is_cached_and_the_turn_state_follows_it(router):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return sse(events(reply("ok")))

    await use(router, handler)
    prompt = PromptText("PERSONA\n\nTURN STATE", "PERSONA")
    messages = [
        {"role": "system", "content": "[Earlier conversation summary: s]"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]
    await turn(router, prompt, messages)
    body = seen[0]
    assert body["system"] == [
        {"type": "text", "text": "PERSONA", "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    ]
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["messages"][1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    final = body["messages"][-1]["content"]
    assert final[0]["text"].startswith(anthropic_chat.CONTEXT_LABEL)
    assert "Earlier conversation summary" in final[0]["text"]
    assert "TURN STATE" in final[0]["text"]
    assert final[-1] == {"type": "text", "text": "three"}


def test_chat_body_keeps_roles_alternating_and_starts_with_the_user():
    body = anthropic_chat.chat_body(
        model="m",
        effort="xhigh",
        persona="P",
        turns=[
            {"role": "assistant", "content": "earlier"},
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": " "},
            {"role": "system", "content": "skip"},
        ],
        volatile="",
        max_tokens=10,
    )
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert body["messages"][2]["content"][0]["text"] == "a\nb"
    empty = anthropic_chat.chat_body(
        model="m", effort="low", persona="P", turns=[], volatile="", max_tokens=1
    )
    assert empty["messages"] == [{"role": "user", "content": [{"type": "text", "text": "."}]}]


def test_timeouts_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("KLUKAI_CLAUDE_TOTAL_S", "45")
    monkeypatch.setenv("KLUKAI_CLAUDE_FIRST_TEXT_S", "nope")
    monkeypatch.setenv("KLUKAI_CLAUDE_FIRST_EVENT_S", "-1")
    assert anthropic_chat.total_s() == 45
    assert anthropic_chat.first_text_s() == 30
    assert anthropic_chat.first_event_s() == 10


def test_a_server_error_alone_does_not_back_off():
    anthropic_chat.note_status(500, None)
    assert not anthropic_chat.backing_off()
    anthropic_chat.note_status(529, "soon")
    assert anthropic_chat.backing_off()


async def test_the_tool_loop_records_its_charge_and_honours_backoff(router, monkeypatch):
    from tests.anthropic_sse import respond

    usage = {"input_tokens": 10, "output_tokens": 8}

    def handler(request):
        return respond(
            request,
            {
                "type": "message",
                "role": "assistant",
                "model": HAIKU_DATED,
                "content": [{"type": "text", "text": "Done."}],
                "stop_reason": "end_turn",
                "usage": usage,
            },
        )

    await use(router, handler)
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    await router.complete_chat("persona", [{"role": "user", "content": "hi"}], config)
    assert xai_spend.spent_today() == pytest.approx(xai_spend.anthropic_usd(usage))
    await use(router, lambda request: httpx.Response(401))
    grok(monkeypatch)
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    out = await router.complete_chat("persona", [{"role": "user", "content": "hi"}], config)
    assert out["choices"][0]["message"]["content"] == "Grok here."
    assert anthropic_chat.backing_off()
    assert xai_spend.spent_today() == pytest.approx(xai_spend.anthropic_usd(usage))


async def test_the_tool_loop_skips_claude_when_capped(router, monkeypatch):
    monkeypatch.setenv("KLUKAI_API_DAILY_USD_CAP", "0")
    grok(monkeypatch)
    sent = []
    await use(router, lambda request: sent.append(request) or httpx.Response(200))
    config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
    out = await router.complete_chat("persona", [{"role": "user", "content": "hi"}], config)
    assert out["choices"][0]["message"]["content"] == "Grok here."
    assert sent == []
