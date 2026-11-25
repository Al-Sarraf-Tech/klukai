"""Cut-off replies, the Grok fallback's persona, and a spent cap.

A max_tokens stop is not a finished line. The CLI fallback speaks as the
stable persona and the done frame names grok. A ledger that is already
spent stays closed when the hold cannot be written; a ledger that cannot
be opened still does not block the call.
"""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from app import xai_spend
from app.llm_completions import LLMCompletionsMixin
from app.llm_router import GROK_MODEL, LLMRouter, LLMStreamFailed
from app.models import LLMConfig, SessionState
from app.personality.system_prompt import PromptText
from app.xai_chat import XaiChatError, complete_text
from tests.anthropic_sse import body as sse_body


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("KLUKAI_XAI_LEDGER", str(tmp_path / "spend.sqlite3"))
    monkeypatch.setenv("KLUKAI_API_DAILY_USD_CAP", "5")
    monkeypatch.setenv("KLUKAI_CHAT_BACKEND", "cli")
    monkeypatch.delenv("KLUKAI_XAI_API_KEY", raising=False)


def test_a_spent_ledger_stays_closed_when_the_hold_cannot_be_written(
    ledger, monkeypatch, tmp_path
):
    xai_spend.record("chat", 5.0)

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("readonly")

    monkeypatch.setattr(xai_spend, "reserve", locked)
    assert xai_spend.try_reserve("chat", 0.01) == (False, None)
    # No file: the ledger cannot be opened, so the call is not blocked.
    monkeypatch.setenv("KLUKAI_XAI_LEDGER", str(tmp_path / "missing.sqlite3"))
    assert xai_spend.try_reserve("chat", 0.01) == (True, None)


async def _router(monkeypatch):
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    router = LLMRouter()
    await router.init()
    return router


async def test_a_max_tokens_stop_is_not_a_successful_short_reply(ledger, monkeypatch):
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
            "delta": {"type": "text_delta", "text": "Hi"},
        },
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {}},
        {"type": "message_stop"},
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
    assert got == ["Hi"]
    assert calls == []


async def test_grok_fallback_keeps_her_persona_and_names_the_model(ledger, monkeypatch):
    seen = {}

    async def cli(system, prompt, *, timeout=120, role="chat"):
        seen["system"] = system
        seen["prompt"] = prompt
        yield "Hello"

    monkeypatch.setattr("app.grok_chat.iter_text", cli)
    config = LLMConfig(provider="anthropic", model="claude-haiku-5-5", base_url="")
    prompt = PromptText("NOT THE PREFIX\n\nTURN", "STATIC PERSONA")
    pieces = [
        piece
        async for piece in LLMCompletionsMixin._stream_grok(
            object(),
            prompt,
            [{"role": "user", "content": "hi"}],
            config,
        )
    ]
    assert pieces == ["Hello"]
    assert seen["system"] == "STATIC PERSONA"
    assert "NOT THE PREFIX" in seen["prompt"]
    assert "user: hi" in seen["prompt"]
    assert (config.provider, config.model) == ("grok", GROK_MODEL)


def _sse(*events: dict) -> str:
    lines = ["data: " + json.dumps(event) for event in events]
    lines.append("data: [DONE]")
    return "\n".join(lines) + "\n"


def _install_xai(monkeypatch, text: str):
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=text)

    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda **_kwargs: httpx.MockTransport(respond)
    )


async def test_a_token_limit_after_the_first_token_does_not_append_a_second_reply(
    ledger, monkeypatch
):
    monkeypatch.setenv("KLUKAI_CHAT_BACKEND", "api")
    monkeypatch.setenv("KLUKAI_XAI_API_KEY", "unit-test-key")
    _install_xai(
        monkeypatch,
        _sse(
            {"choices": [{"delta": {"content": "Hi"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
        ),
    )
    calls = []

    async def cli(*_args, **_kwargs):
        calls.append("cli")
        yield "from cli"

    monkeypatch.setattr("app.grok_chat.iter_text", cli)
    got = []
    with pytest.raises(LLMStreamFailed):
        async for piece in LLMCompletionsMixin._stream_grok(
            object(), "persona", [{"role": "user", "content": "hi"}]
        ):
            got.append(piece)
    assert got == ["Hi"]
    assert calls == []


async def test_a_story_cut_off_by_max_tokens_is_not_returned(ledger, monkeypatch):
    monkeypatch.setenv("KLUKAI_CHAT_BACKEND", "api")
    monkeypatch.setenv("KLUKAI_XAI_API_KEY", "unit-test-key")

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "She woke"},
                        "finish_reason": "max_tokens",
                    }
                ]
            },
        )

    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda **_kwargs: httpx.MockTransport(respond)
    )
    with pytest.raises(XaiChatError, match="token limit"):
        await complete_text("s", "p")
