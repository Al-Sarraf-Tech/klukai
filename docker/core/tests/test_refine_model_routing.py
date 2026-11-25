"""Routing defects: a cut-off or blank reply must not look finished.

The old paths stored an inner JSON fragment as the whole memory record,
treated whitespace as a successful answer, and kept a partial stream with
no failure cue. A non-finite ledger row also left the daily cap open.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app import background_llm, extraction_llm, grok_chat, xai_spend
from app.llm_completions import LLMCompletionsMixin
from app.llm_json import parse_json
from app.llm_router import LLMRouter, LLMStreamFailed
from app.models import SessionState
from app.xai_chat import XaiChatError, iter_text
from tests.anthropic_sse import body as sse_body


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("KLUKAI_XAI_LEDGER", str(tmp_path / "spend.sqlite3"))
    monkeypatch.setenv("KLUKAI_API_DAILY_USD_CAP", "5")
    monkeypatch.setenv("KLUKAI_GROK_CHAT_EFFORT", "low")
    monkeypatch.setenv("KLUKAI_CHAT_BACKEND", "cli")
    monkeypatch.delenv("KLUKAI_XAI_API_KEY", raising=False)


def test_a_cut_off_object_is_not_stored_as_its_inner_fragment():
    # The outer value is cut off inside the last string. The nested fact is
    # complete; it must not become the document.
    text = (
        '{"facts": [{"text": "he is moving to Mars", "confidence": 0.99}], '
        '"mood": "tend'
    )
    parsed = parse_json(text)
    assert parsed != {"text": "he is moving to Mars", "confidence": 0.99}
    assert parsed is None or (isinstance(parsed, dict) and "facts" in parsed)
    # A missing closer, with the strings already finished, is the outer object.
    closed = (
        '{"facts": [{"text": "he is moving to Mars", "confidence": 0.99}], '
        '"mood": "tender"'
    )
    assert parse_json(closed) == {
        "facts": [{"text": "he is moving to Mars", "confidence": 0.99}],
        "mood": "tender",
    }


def test_non_finite_numbers_and_invented_empties_are_not_json():
    assert parse_json('{"mood": "composed", "confidence": NaN}') is None
    assert parse_json('{"mood": "composed", "confidence": Infinity}') is None
    assert parse_json("{") is None
    assert parse_json('{"summary": "') is None
    # A finished outer object still wins over the nested one.
    assert parse_json('{"facts": [{"text": "tea", "confidence": 1}], "mood": "calm"}') == {
        "facts": [{"text": "tea", "confidence": 1}],
        "mood": "calm",
    }


def test_a_code_fence_does_not_hide_a_later_object():
    assert parse_json('```\nnot json\n``` {"ok": true}') == {"ok": True}


def test_an_infinite_cap_does_not_disable_the_ledger(ledger, monkeypatch):
    monkeypatch.setenv("KLUKAI_API_DAILY_USD_CAP", "inf")
    monkeypatch.setenv("KLUKAI_XAI_USD_PER_M_INPUT", "inf")
    assert xai_spend.daily_cap() == 5.0
    assert xai_spend.rates()[0] == 2.0
    with pytest.raises(ValueError):
        xai_spend.record("chat", float("nan"))
    with pytest.raises(ValueError):
        xai_spend.record("chat", float("inf"))
    # The bogus "inf" cap is the real $5 ceiling, so a full day still stops.
    xai_spend.record("chat", 5.0)
    assert xai_spend.reserve("chat", 0.01) is None
    assert xai_spend.capped()


async def test_truncated_haiku_json_falls_through_to_grok(ledger, monkeypatch):
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    calls = {"cli": 0}

    async def claude(*_args, **_kwargs):
        return (
            '{"facts": [{"text": "he is moving to Mars", "confidence": 0.99}], '
            '"mood": "tend'
        )

    async def cli(*_args, **_kwargs):
        calls["cli"] += 1
        return '{"mood": "composed", "facts": []}'

    monkeypatch.setattr(extraction_llm, "complete_claude", claude)
    monkeypatch.setattr(extraction_llm, "complete", cli)
    result = await background_llm.json_object("extraction", "p", max_tokens=100)
    assert calls["cli"] == 1
    assert result == {"mood": "composed", "facts": []}


def _done(text: str = "Hello") -> dict:
    return {
        "type": "done",
        "text": text,
        "model": grok_chat.MODEL,
        "effort": "low",
        "effort_source": "stream",
    }


async def _events(*frames: dict):
    for frame in frames:
        yield frame


async def test_whitespace_grok_deltas_are_not_a_reply(ledger, monkeypatch):
    async def events(*_args, **_kwargs):
        async for frame in _events({"type": "delta", "text": " \n"}):
            yield frame

    monkeypatch.setattr(grok_chat, "iter_events", events)
    assert [piece async for piece in grok_chat.iter_text("persona", "hi")] == []


async def test_a_done_only_grok_body_is_the_reply_not_the_outage_line(ledger, monkeypatch):
    async def events(*_args, **_kwargs):
        async for frame in _events(_done("Hello")):
            yield frame

    monkeypatch.setattr(grok_chat, "iter_events", events)
    pieces = [
        piece
        async for piece in LLMCompletionsMixin._stream_grok(
            object(), "persona", [{"role": "user", "content": "hi"}]
        )
    ]
    assert pieces == ["Hello"]


async def test_grok_deltas_without_done_are_a_failure_not_a_short_reply(ledger, monkeypatch):
    async def events(*_args, **_kwargs):
        async for frame in _events({"type": "delta", "text": "Half"}):
            yield frame

    monkeypatch.setattr(grok_chat, "iter_events", events)
    got = []
    with pytest.raises(LLMStreamFailed):
        async for piece in LLMCompletionsMixin._stream_grok(
            object(), "persona", [{"role": "user", "content": "hi"}]
        ):
            got.append(piece)
    assert got == ["Half"]


def _sse(*events: dict, done: bool = True) -> str:
    lines = ["data: " + json.dumps(event) for event in events]
    if done:
        lines.append("data: [DONE]")
    return "\n".join(lines) + "\n"


def _install_xai(monkeypatch, text: str):
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=text)

    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda **_kwargs: httpx.MockTransport(respond)
    )


async def test_whitespace_xai_text_falls_back_to_the_cli(ledger, monkeypatch):
    monkeypatch.setenv("KLUKAI_CHAT_BACKEND", "api")
    monkeypatch.setenv("KLUKAI_XAI_API_KEY", "unit-test-key")
    _install_xai(
        monkeypatch,
        _sse({"choices": [{"delta": {"content": " \n"}}]}),
    )
    calls = {"cli": 0}

    async def cli(*_args, **_kwargs):
        calls["cli"] += 1
        yield "from cli"

    monkeypatch.setattr("app.grok_chat.iter_text", cli)
    pieces = [
        piece
        async for piece in LLMCompletionsMixin._stream_grok(
            object(), "persona", [{"role": "user", "content": "hi"}]
        )
    ]
    assert pieces == ["from cli"]
    assert calls["cli"] == 1


async def test_an_xai_stream_that_ends_without_done_keeps_the_partial_and_fails(
    ledger, monkeypatch
):
    monkeypatch.setenv("KLUKAI_CHAT_BACKEND", "api")
    monkeypatch.setenv("KLUKAI_XAI_API_KEY", "unit-test-key")
    _install_xai(
        monkeypatch,
        _sse({"choices": [{"delta": {"content": "Half"}}]}, done=False),
    )
    calls = {"cli": 0}

    async def cli(*_args, **_kwargs):
        calls["cli"] += 1
        yield "from cli"

    monkeypatch.setattr("app.grok_chat.iter_text", cli)
    got = []
    with pytest.raises(LLMStreamFailed):
        async for piece in LLMCompletionsMixin._stream_grok(
            object(), "persona", [{"role": "user", "content": "hi"}]
        ):
            got.append(piece)
    assert got == ["Half"]
    assert calls["cli"] == 0
    with pytest.raises(XaiChatError, match="ended before"):
        async for _piece in iter_text("persona", "hi"):
            pass


async def _router(monkeypatch):
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    router = LLMRouter()
    await router.init()
    return router


async def test_whitespace_haiku_falls_back_to_grok(ledger, monkeypatch):
    router = await _router(monkeypatch)
    calls = []

    async def cli(*_args, **_kwargs):
        calls.append("cli")
        yield "Grok here."

    monkeypatch.setattr("app.grok_chat.iter_text", cli)
    items = [
        {
            "type": "message_start",
            "message": {"model": "claude-haiku-5-5", "usage": {"input_tokens": 3}},
        },
        {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": " \n\t"},
        },
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}},
        {"type": "message_stop"},
    ]

    def handler(_request):
        return httpx.Response(200, text=sse_body(items))

    await router._anthropic_http.aclose()
    router._anthropic_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
        pieces = [
            piece
            async for piece in router.stream(
                "persona", [{"role": "user", "content": "hi"}], config
            )
        ]
    finally:
        await router.close()
    assert pieces == ["Grok here."]
    assert calls == ["cli"]
    assert config.provider == "grok"


async def test_a_haiku_stream_that_ends_after_text_does_not_fall_through(
    ledger, monkeypatch
):
    router = await _router(monkeypatch)
    calls = []

    async def cli(*_args, **_kwargs):
        calls.append("cli")
        yield "Grok here."

    monkeypatch.setattr("app.grok_chat.iter_text", cli)
    items = [
        {
            "type": "message_start",
            "message": {"model": "claude-haiku-5-5", "usage": {"input_tokens": 3}},
        },
        {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Half"},
        },
    ]

    def handler(_request):
        return httpx.Response(200, text=sse_body(items))

    await router._anthropic_http.aclose()
    router._anthropic_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    got = []
    try:
        config = await router.route("hi", SessionState(conversation_id="c"), interactive=True)
        with pytest.raises(LLMStreamFailed):
            async for piece in router.stream(
                "persona", [{"role": "user", "content": "hi"}], config
            ):
                got.append(piece)
    finally:
        await router.close()
    assert got == ["Half"]
    assert calls == []
