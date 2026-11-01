"""Background text on Claude Haiku 5.5 at low effort, with the Grok CLI as backup.

Owner decision 2026-11-01: the Grok account's usage ran out and her memory
stopped, so background work moved to the Claude key chat already uses.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import background_llm as bg
from app import circuit_breakers
from app import extraction_llm as xl
from app.creative_budget import CreativeBudgetError
from app.extraction_llm import ExtractionCancelled, ExtractionLLMError

KEY = "test-claude-background-key"
SECRET_PROMPT = "the Commander's private words"
SECRET_REPLY = '{"mood": "serene"}'


def _message(**overrides):
    return {
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "stop_reason": "end_turn",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "s"},
            {"type": "text", "text": SECRET_REPLY},
        ],
        "usage": {"input_tokens": 900, "output_tokens": 300},
        **overrides,
    }


@pytest.fixture
def claude_on(monkeypatch):
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.delenv("KLUKAI_EXTRACTION_MODEL", raising=False)
    monkeypatch.setattr(xl, "_warned", set())


@pytest.fixture
def api(monkeypatch, claude_on):
    """Answer the Messages endpoint locally and record every request."""
    monkeypatch.setattr(circuit_breakers, "_BREAKERS", {})
    state = SimpleNamespace(
        requests=[],
        reply=lambda request: httpx.Response(200, json=_message(), request=request),
        reserve=AsyncMock(return_value="attempt-1"),
        record=AsyncMock(),
    )
    monkeypatch.setattr(xl, "reserve_attempt", state.reserve)
    monkeypatch.setattr(xl, "record_usage", state.record)

    async def send(self, request, *args, **kwargs):
        assert state.reserve.await_count == 1, "reserve before anything is sent"
        state.requests.append(request)
        return state.reply(request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    return state


@pytest.fixture
def routes(monkeypatch, claude_on):
    """Stand in for the two model calls, to test the order between them."""
    state = SimpleNamespace(
        claude=AsyncMock(return_value=SECRET_REPLY),
        cli=AsyncMock(return_value='{"mood": "wary"}'),
    )
    monkeypatch.setattr(xl, "complete_claude", state.claude)
    monkeypatch.setattr(xl, "complete", state.cli)
    return state


def _lines(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Background model:")
    ]


# ── the switch ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("switch", "key"), [("true", ""), ("false", KEY), ("", KEY), ("false", "")]
)
def test_background_stays_on_grok_without_both_settings(monkeypatch, switch, key):
    monkeypatch.setattr(xl, "_warned", set())
    monkeypatch.delenv("KLUKAI_EXTRACTION_MODEL", raising=False)
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", switch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    assert xl.uses_anthropic() is False
    assert (xl.provider(), xl.model()) == ("grok", "grok-4.7")


def test_both_settings_turn_on_haiku(claude_on):
    assert xl.uses_anthropic() is True
    assert (xl.provider(), xl.model()) == ("anthropic", "claude-haiku-5-5")


def test_a_newer_haiku_id_is_honored(claude_on, monkeypatch):
    monkeypatch.setenv("KLUKAI_EXTRACTION_MODEL", "claude-haiku-5-6")
    assert xl.model() == "claude-haiku-5-6"


@pytest.mark.parametrize("value", ["claude-sonnet-5-5", "claude-opus-5-5", "grok-4.7"])
def test_only_a_haiku_id_can_be_billed(claude_on, monkeypatch, value, caplog):
    monkeypatch.setenv("KLUKAI_EXTRACTION_MODEL", value)
    with caplog.at_level(logging.WARNING, logger="app.extraction_llm"):
        assert xl.model() == "claude-haiku-5-5"
        assert xl.model() == "claude-haiku-5-5"
    assert len(caplog.records) == 1


def test_startup_line_names_haiku_and_its_backup(claude_on):
    line = xl.startup_line()
    assert "claude-haiku-5-5" in line and "low" in line and "Grok CLI" in line


def test_startup_line_names_grok_when_claude_is_off(monkeypatch):
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "false")
    monkeypatch.delenv("KLUKAI_EXTRACTION_MODEL", raising=False)
    line = xl.startup_line()
    assert "grok-4.7" in line and "claude" not in line


# ── one Haiku request ─────────────────────────────────────────────────────


async def test_request_shape_and_visible_text(api):
    text = await xl.complete_claude("Classify this.", max_tokens=3000, route="extraction")
    assert text == SECRET_REPLY
    request = api.requests[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == KEY
    assert json.loads(request.content) == {
        "model": "claude-haiku-5-5",
        "system": xl.SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Classify this."}]}
        ],
        "max_tokens": 3000,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "low"},
    }
    api.reserve.assert_awaited_once_with("extraction")
    api.record.assert_awaited_once_with(
        "attempt-1", {"input_tokens": 900, "output_tokens": 300}
    )


async def test_a_caller_system_prompt_replaces_the_default(api):
    await xl.complete_claude("x", max_tokens=100, route="dream", system="You are Klukai.")
    assert json.loads(api.requests[0].content)["system"] == "You are Klukai."


async def test_cap_denial_sends_nothing(api):
    api.reserve.side_effect = CreativeBudgetError("capped")
    with pytest.raises(xl.ExtractionUnavailable):
        await xl.complete_claude("x", max_tokens=100, route="extraction")
    assert api.requests == []


async def test_no_key_sends_nothing(api, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(xl.ExtractionUnavailable):
        await xl.complete_claude("x", max_tokens=100, route="extraction")
    api.reserve.assert_not_awaited()
    assert api.requests == []


@pytest.mark.parametrize(
    ("prompt", "max_tokens", "system"),
    [
        ("", 100, None),
        ("   ", 100, None),
        ("x" * (xl.MAX_PROMPT_CHARS + 1), 100, None),
        ("x", 0, None),
        ("x", xl.MAX_REQUEST_TOKENS + 1, None),
        ("x", True, None),
        ("x", 100, "   "),
        ("x", 100, "s" * (xl.MAX_SYSTEM_CHARS + 1)),
    ],
)
async def test_invalid_requests_are_not_billed(api, prompt, max_tokens, system):
    with pytest.raises(ExtractionLLMError):
        await xl.complete_claude(
            prompt, max_tokens=max_tokens, route="extraction", system=system
        )
    api.reserve.assert_not_awaited()
    assert api.requests == []


@pytest.mark.parametrize(
    "reply",
    [
        _message(stop_reason="refusal"),
        _message(stop_reason="max_tokens"),
        _message(content=[]),
        _message(content=[{"type": "text", "text": "   "}]),
        _message(content=[{"type": "tool_use", "id": "t", "name": "n", "input": {}}]),
        _message(content=["not a block"]),
        _message(content=[{"type": "text", "text": 7}]),
        _message(content="not a list"),
        _message(model=""),
        _message(usage={"input_tokens": -1}),
        _message(usage="not a dict"),
        {"type": "error"},
        ["not a message"],
    ],
)
async def test_an_unusable_reply_is_an_error_and_its_attempt_stays_spent(api, reply):
    api.reply = lambda request: httpx.Response(200, json=reply, request=request)
    with pytest.raises(ExtractionLLMError):
        await xl.complete_claude("x", max_tokens=100, route="extraction")
    api.reserve.assert_awaited_once()
    api.record.assert_not_awaited()


@pytest.mark.parametrize("status", [307, 400, 401, 429, 500, 529])
async def test_http_failures_are_free_of_credentials_and_content(api, status):
    body = {"error": {"message": f"{KEY} {SECRET_PROMPT}"}}
    api.reply = lambda request: httpx.Response(status, json=body, request=request)
    with pytest.raises(ExtractionLLMError) as raised:
        await xl.complete_claude(SECRET_PROMPT, max_tokens=100, route="extraction")
    assert len(api.requests) == 1  # a redirect is not followed
    message = str(raised.value)
    assert f"HTTP {status}" in message
    assert KEY not in message and SECRET_PROMPT not in message


async def test_oversized_reply_is_rejected(api):
    size = xl._MAX_RESPONSE_BYTES + 1
    api.reply = lambda request: httpx.Response(200, content=b"x" * size, request=request)
    with pytest.raises(ExtractionLLMError, match="size"):
        await xl.complete_claude("x", max_tokens=100, route="extraction")


async def test_a_connection_failure_is_an_error(api):
    def fail(request):
        raise httpx.ConnectError("down", request=request)

    api.reply = fail
    with pytest.raises(ExtractionLLMError, match="ConnectError"):
        await xl.complete_claude("x", max_tokens=100, route="extraction")


async def test_failures_count_on_their_own_breaker_not_chats(api):
    api.reply = lambda request: httpx.Response(500, request=request)
    with pytest.raises(ExtractionLLMError):
        await xl.complete_claude("x", max_tokens=100, route="extraction")
    assert "anthropic_extraction" in circuit_breakers.all_breakers()
    assert "anthropic" not in circuit_breakers.all_breakers()


async def test_telemetry_failure_never_fails_a_paid_call(api, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr("app.observability.record_llm_usage", boom)
    assert await xl.complete_claude("x", max_tokens=100, route="dream") == SECRET_REPLY


async def test_usage_is_reported_with_all_input_tokens(api, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "app.observability.record_llm_usage", lambda **kwargs: seen.update(kwargs)
    )
    usage = {
        "input_tokens": 10,
        "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 30,
        "output_tokens": 5,
    }
    api.reply = lambda request: httpx.Response(
        200, json=_message(usage=usage), request=request
    )
    await xl.complete_claude("x", max_tokens=100, route="dream")
    assert seen["model"] == "claude-haiku-5-5" and seen["route"] == "dream"
    assert (seen["tokens_in"], seen["tokens_out"]) == (60, 5)


@pytest.mark.parametrize(
    ("role", "timeout", "expected"),
    [
        ("background", None, xl.REQUEST_TIMEOUT_S),
        ("recall", None, "recall"),
        ("recall", 30.0, 30.0),
        ("background", 0, xl.REQUEST_TIMEOUT_S),
    ],
)
async def test_haiku_keeps_the_callers_time_budget(api, monkeypatch, role, timeout, expected):
    from app.memory_settings import RECALL_SELECTION_TIMEOUT_S

    budgets = []
    real = xl.asyncio.timeout

    def timed(seconds):
        budgets.append(seconds)
        return real(seconds)

    monkeypatch.setattr(xl.asyncio, "timeout", timed)
    await xl.complete_claude("x", max_tokens=100, route="r", role=role, timeout=timeout)
    assert budgets == [RECALL_SELECTION_TIMEOUT_S if expected == "recall" else expected]


# ── the route: Haiku, then the CLI, then the caller's default ─────────────


async def test_haiku_answers_and_the_cli_is_not_called(routes, caplog):
    report = xl.CallReport()
    with caplog.at_level(logging.INFO, logger="app.background_llm"):
        out = await bg.json_object("extraction", SECRET_PROMPT, max_tokens=400, report=report)
    assert out == {"mood": "serene"}
    routes.cli.assert_not_awaited()
    assert (report.provider, report.model, report.outcome) == (
        "anthropic",
        "claude-haiku-5-5",
        "ok",
    )
    assert _lines(caplog) == [
        "Background model: caller=extraction provider=anthropic "
        "model=claude-haiku-5-5 outcome=ok"
    ]
    for record in caplog.records:
        assert SECRET_PROMPT not in record.getMessage()
        assert "serene" not in record.getMessage()


async def test_arguments_reach_haiku(routes):
    await bg.text(
        "weekly_reflection",
        "write",
        system="You are Klukai.",
        max_tokens=400,
        haiku_max_tokens=1234,
    )
    routes.claude.assert_awaited_once_with(
        "write",
        max_tokens=1234,
        route="weekly_reflection",
        system="You are Klukai.",
    )


async def test_role_and_timeout_reach_haiku(routes):
    await bg.json_object("memory_recall", "pick", max_tokens=100, role="recall", timeout=30.0)
    routes.claude.assert_awaited_once_with(
        "pick",
        max_tokens=100 + bg.THINKING_HEADROOM_TOKENS,
        route="memory_recall",
        role="recall",
        timeout=30.0,
    )


@pytest.mark.parametrize(
    ("failure", "outcome"),
    [
        (ExtractionLLMError("Haiku request failed"), "failed->cli"),
        (xl.ExtractionUnavailable("capped"), "unavailable->cli"),
        (RuntimeError("anything"), "failed->cli"),
    ],
)
async def test_a_haiku_failure_hands_the_call_to_the_cli(routes, caplog, failure, outcome):
    routes.claude.side_effect = failure
    report = xl.CallReport()
    with caplog.at_level(logging.INFO, logger="app.background_llm"):
        out = await bg.json_object("extraction", "p", max_tokens=400, report=report)
    assert out == {"mood": "wary"}
    routes.cli.assert_awaited_once()
    assert (report.provider, report.model, report.outcome) == ("grok", "grok-4.7", "ok")
    assert _lines(caplog) == [
        f"Background model: caller=extraction provider=anthropic "
        f"model=claude-haiku-5-5 outcome={outcome}",
        "Background model: caller=extraction provider=grok model=grok-4.7 outcome=ok",
    ]


async def test_an_unparsable_haiku_reply_hands_the_call_to_the_cli(routes, caplog):
    routes.claude.return_value = "not json"
    with caplog.at_level(logging.INFO, logger="app.background_llm"):
        out = await bg.json_object("extraction", "p", max_tokens=400)
    assert out == {"mood": "wary"}
    assert "outcome=unparsable->cli" in _lines(caplog)[0]


async def test_when_both_fail_the_caller_keeps_its_safe_default(routes):
    routes.claude.side_effect = ExtractionLLMError("down")
    routes.cli.side_effect = ExtractionLLMError("402")
    report = xl.CallReport()
    assert await bg.json_object("extraction", "p", max_tokens=400, report=report) is None
    assert await bg.text("dream", "p", max_tokens=400) == ""
    # memory_extraction keeps her mood as it was when the outcome ends in defaults.
    assert report.outcome == "failed->defaults"
    assert report.provider == "grok"


async def test_a_cli_cancel_after_a_haiku_failure_still_requeues(routes):
    routes.claude.side_effect = ExtractionLLMError("down")
    routes.cli.side_effect = ExtractionCancelled("cancelled")
    with pytest.raises(ExtractionCancelled):
        await bg.json_object("extraction", "p", max_tokens=400, requeue=True)


async def test_with_claude_off_haiku_is_never_called(routes, monkeypatch):
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "false")
    report = xl.CallReport()
    assert await bg.json_object("extraction", "p", max_tokens=400, report=report) == {
        "mood": "wary"
    }
    routes.claude.assert_not_awaited()
    assert (report.provider, report.model) == ("grok", "grok-4.7")


async def test_stories_still_try_the_xai_api_first(routes, monkeypatch):
    monkeypatch.setattr("app.xai_spend.api_key", lambda: "xai-key")
    monkeypatch.setattr(
        "app.xai_chat.complete_text", AsyncMock(return_value='{"annotation": "kept"}')
    )
    assert await bg.json_object("her_pov", "p", max_tokens=512) == {"annotation": "kept"}
    routes.claude.assert_not_awaited()
    routes.cli.assert_not_awaited()


async def test_a_failed_story_call_falls_to_haiku_before_the_cli(routes, monkeypatch):
    from app.xai_chat import XaiChatError

    monkeypatch.setattr("app.xai_spend.api_key", lambda: "xai-key")
    monkeypatch.setattr(
        "app.xai_chat.complete_text", AsyncMock(side_effect=XaiChatError("down"))
    )
    assert await bg.json_object("her_pov", "p", max_tokens=512) == {"mood": "serene"}
    routes.claude.assert_awaited_once()
    routes.cli.assert_not_awaited()


async def test_end_to_end_a_real_haiku_request_through_the_route(api):
    report = xl.CallReport()
    out = await bg.json_object("extraction", "p", max_tokens=400, report=report)
    assert out == {"mood": "serene"}
    assert report.provider == "anthropic"
    assert len(api.requests) == 1
    assert json.loads(api.requests[0].content)["output_config"] == {"effort": "low"}
