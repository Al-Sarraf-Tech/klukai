"""The background route with Claude off: the Grok CLI, else the caller's safe default.

test_haiku_background.py covers the route with Claude on.
"""

from __future__ import annotations

import inspect
import json
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from app import background_llm as bg
from app import extraction_llm as xl

SECRET_PROMPT = "the Commander's private words"
SECRET_REPLY = "her private reply"


@pytest.fixture
def haiku(monkeypatch):
    monkeypatch.delenv("KLUKAI_EXTRACTION_MODEL", raising=False)
    complete = AsyncMock(return_value=SECRET_REPLY)
    monkeypatch.setattr(xl, "complete", complete)
    return complete


@pytest.fixture
def no_network(monkeypatch):
    """Record every HTTP request any client tries to send (there must be none)."""
    sent: list[httpx.Request] = []

    async def send(self, request, *args, **kwargs):
        sent.append(request)
        raise httpx.ConnectError("no network in this test", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    return sent


def _lines(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("Background model:")
    ]


# ── budget ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("visible", "expected"),
    [(150, 150 + bg.THINKING_HEADROOM_TOKENS), (640, 3712), (6000, 8192), (0, 3072)],
)
def test_haiku_budget_adds_thinking_headroom_and_caps(visible, expected):
    assert bg.haiku_budget(visible) == expected


# ── no local route ───────────────────────────────────────────────────────


@pytest.mark.parametrize("entry", [bg.text, bg.json_object])
def test_callers_cannot_attach_a_local_fallback(entry):
    params = inspect.signature(entry).parameters
    assert "local" not in params
    assert "local_model" not in params


@pytest.mark.parametrize("entry", [bg.text, bg.json_object])
async def test_a_local_request_argument_is_rejected(haiku, entry):
    local = AsyncMock(return_value="local line")
    with pytest.raises(TypeError):
        await entry("dream", "p", max_tokens=200, local=local)
    local.assert_not_awaited()
    haiku.assert_not_awaited()


def test_the_route_has_no_local_model_knobs():
    for name in (
        "local_circuit_closed",
        "LOCAL_MODEL",
        "LOCAL_FALLBACK_DEADLINE_SECONDS",
        "LOCAL_PRIMARY_DEADLINE_SECONDS",
    ):
        assert not hasattr(bg, name), name


# ── Haiku answers ────────────────────────────────────────────────────────


async def test_haiku_answers_and_logs_one_content_free_line(haiku, caplog):
    report = xl.CallReport()
    with caplog.at_level(logging.INFO, logger="app.background_llm"):
        out = await bg.text("mission_update", SECRET_PROMPT, max_tokens=150, report=report)
    assert out == SECRET_REPLY
    haiku.assert_awaited_once_with(
        SECRET_PROMPT,
        max_tokens=150 + bg.THINKING_HEADROOM_TOKENS,
        route="mission_update",
    )
    assert (report.provider, report.model, report.outcome) == (
        "grok",
        "grok-4.7",
        "ok",
    )
    assert _lines(caplog) == [
        "Background model: caller=mission_update provider=grok "
        "model=grok-4.7 outcome=ok"
    ]
    # Content never reaches any log line from this module.
    for record in caplog.records:
        assert SECRET_PROMPT not in record.getMessage()
        assert SECRET_REPLY not in record.getMessage()


async def test_configured_haiku_model_is_reported(haiku, monkeypatch):
    monkeypatch.setenv("KLUKAI_EXTRACTION_MODEL", "claude-haiku-5-6")
    report = xl.CallReport()
    await bg.text("dream", "p", max_tokens=200, report=report)
    assert (report.provider, report.model) == ("grok", "grok-4.7")


async def test_system_prompt_and_explicit_budget_are_forwarded(haiku):
    await bg.text(
        "weekly_reflection",
        "write",
        system="You are Klukai.",
        max_tokens=400,
        haiku_max_tokens=1234,
    )
    haiku.assert_awaited_once_with(
        "write",
        max_tokens=1234,
        route="weekly_reflection",
        system="You are Klukai.",
    )


async def test_report_is_optional(haiku):
    assert await bg.text("dream", "p", max_tokens=10) == SECRET_REPLY


# ── Haiku problems: safe default, nothing else is tried ──────────────────


@pytest.mark.parametrize(
    ("effect", "reason"),
    [
        (xl.ExtractionUnavailable("cap"), "unavailable"),
        (xl.ExtractionLLMError("boom"), "failed"),
        (RuntimeError("unexpected"), "failed"),
    ],
)
async def test_haiku_problem_returns_the_default_without_a_local_request(
    haiku, no_network, effect, reason, caplog
):
    haiku.side_effect = effect
    report = xl.CallReport()
    with caplog.at_level(logging.INFO, logger="app.background_llm"):
        out = await bg.text("dream", SECRET_PROMPT, max_tokens=200, report=report)
    assert out == ""
    haiku.assert_awaited_once()
    assert no_network == []
    assert (report.provider, report.model) == ("grok", "grok-4.7")
    assert report.outcome == f"{reason}->defaults"
    assert _lines(caplog) == [
        "Background model: caller=dream provider=grok "
        f"model=grok-4.7 outcome={reason}->defaults"
    ]
    for record in caplog.records:
        assert SECRET_PROMPT not in record.getMessage()


@pytest.mark.parametrize(
    "effect",
    [xl.ExtractionUnavailable("cap"), xl.ExtractionLLMError("boom"), ValueError("x")],
)
async def test_haiku_problem_json_is_none(haiku, no_network, effect):
    haiku.side_effect = effect
    report = xl.CallReport()
    assert await bg.json_object("promises", "p", max_tokens=640, report=report) is None
    haiku.assert_awaited_once()
    assert no_network == []
    assert report.outcome.endswith("->defaults")


@pytest.mark.parametrize("reply", ["", "   ", "\n\t"])
async def test_blank_haiku_text_counts_as_empty(haiku, no_network, reply):
    haiku.return_value = reply
    report = xl.CallReport()
    assert await bg.text("dream", "p", max_tokens=200, report=report) == ""
    assert report.outcome == "empty->defaults"
    assert no_network == []


@pytest.mark.parametrize(
    "reply",
    [
        '{"promises": [], "events": [{"what": "x"}]}',
        'Sure:\n```json\n{"promises": [], "events": [{"what": "x"}]}\n```',
        'Here you go {"promises": [], "events": [{"what": "x"}]} done',
        '{"promises": [], "events": [{"what": "x"},],}',
    ],
)
async def test_haiku_json_is_parsed_tolerantly(haiku, reply):
    haiku.return_value = reply
    report = xl.CallReport()
    out = await bg.json_object("promises", "p", max_tokens=640, report=report)
    assert out == {"promises": [], "events": [{"what": "x"}]}
    assert report.outcome == "ok"


@pytest.mark.parametrize("reply", ["no json here", "[1, 2]", "{}", '"text"', ""])
async def test_haiku_json_without_an_object_is_unparsable_defaults(
    haiku, no_network, reply
):
    haiku.return_value = reply
    report = xl.CallReport()
    out = await bg.json_object("promises", "p", max_tokens=640, report=report)
    assert out is None
    assert report.outcome == "unparsable->defaults"
    assert no_network == []


# ── transport: the system override ───────────────────────────────────────


def _reply():
    return {
        "type": "message",
        "role": "assistant",
        "model": "grok-4.7",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "Welcome back, Commander."}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


@pytest.fixture
def wire(monkeypatch):
    from app.grok_chat import Turn

    monkeypatch.setattr("app.grok_chat.configured", lambda: True)
    reserve = AsyncMock(return_value="attempt-1")
    monkeypatch.setattr(xl, "reserve_attempt", reserve)
    monkeypatch.setattr(xl, "record_usage", AsyncMock())
    seen: list[dict] = []

    async def complete_turn(system, prompt, *, timeout=60, role="chat"):
        seen.append({"system": system, "prompt": prompt})
        return Turn("Welcome back, Commander.", "grok-4.7", "low", "stream")

    monkeypatch.setattr("app.grok_chat.complete_turn", complete_turn)
    return reserve, seen


async def test_default_system_covers_writing_as_well_as_analysis(wire):
    _, seen = wire
    await xl.complete("x", max_tokens=100, route="dream")
    assert seen[0]["system"] == xl.SYSTEM_PROMPT
    assert "in-character" in xl.SYSTEM_PROMPT


async def test_system_override_is_sent_as_given(wire):
    reserve, seen = wire
    out = await xl.complete(
        "greet him", max_tokens=100, route="return_greeting", system="You are Klukai."
    )
    assert out == "Welcome back, Commander."
    assert seen[0]["system"] == "You are Klukai."
    assert seen[0]["prompt"] == "greet him"
    reserve.assert_awaited_once_with("extraction")


@pytest.mark.parametrize("system", ["", "   ", "s" * (xl.MAX_SYSTEM_CHARS + 1), 5])
async def test_invalid_system_never_reserves(wire, system):
    reserve, seen = wire
    with pytest.raises(xl.ExtractionLLMError):
        await xl.complete("x", max_tokens=100, route="dream", system=system)
    reserve.assert_not_awaited()
    assert seen == []


async def test_without_a_key_nothing_is_sent_and_the_caller_gets_its_default(
    monkeypatch, no_network
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    reserve = AsyncMock(return_value="attempt-1")
    monkeypatch.setattr(xl, "reserve_attempt", reserve)
    report = xl.CallReport()
    assert await bg.text("dream", "p", max_tokens=200, report=report) == ""
    assert await bg.json_object("promises", "p", max_tokens=640) is None
    assert report.outcome == "unavailable->defaults"
    reserve.assert_not_awaited()
    assert no_network == []


# ── one cap for every caller ─────────────────────────────────────────────


async def test_every_caller_spends_the_same_daily_allowance(monkeypatch, tmp_path):
    """Two attempts allowed: the third caller of any kind gets its default."""
    from app import creative_budget as budget

    monkeypatch.setattr("app.grok_chat.configured", lambda: True)
    monkeypatch.setenv("KLUKAI_EXTRACTION_DAILY_LIMIT", "2")
    monkeypatch.setenv("KLUKAI_CREATIVE_LEDGER", str(tmp_path / "l" / "a.sqlite3"))
    monkeypatch.setattr(
        budget, "_utc_now", lambda: datetime(2026, 10, 19, tzinfo=timezone.utc)
    )
    monkeypatch.setattr(xl, "_cap_logged_day", None)
    sent = []

    async def complete_turn(system, prompt, *, timeout=60, role="chat"):
        from app.grok_chat import Turn
        sent.append(prompt)
        return Turn("ok", "grok-4.7", "low", "stream")

    monkeypatch.setattr("app.grok_chat.complete_turn", complete_turn)

    assert await bg.text("dream", "p", max_tokens=200)
    assert await bg.text("memory_recall", "p", max_tokens=160)
    report = xl.CallReport()
    assert await bg.text("mission_update", "p", max_tokens=150, report=report) == ""
    assert report.outcome == "unavailable->defaults"
    assert len(sent) == 2


# ── story and summary may use the API; other callers stay on the CLI ──


async def test_a_story_without_a_key_stays_on_the_cli(haiku, monkeypatch):
    api = AsyncMock(return_value="from api")
    monkeypatch.setattr("app.xai_chat.complete_text", api)
    assert await bg.text("episode_summary", "p", max_tokens=150) == SECRET_REPLY
    api.assert_not_awaited()
    haiku.assert_awaited()


async def test_a_story_on_the_api_skips_the_cli(haiku, monkeypatch, caplog):
    monkeypatch.setenv("KLUKAI_XAI_API_KEY", "unit-test-key")
    api = AsyncMock(return_value="She stayed.")
    monkeypatch.setattr("app.xai_chat.complete_text", api)
    with caplog.at_level(logging.INFO, logger="app.background_llm"):
        out = await bg.text(
            "episode_summary", SECRET_PROMPT, max_tokens=150, system="Be brief."
        )
    assert out == "She stayed."
    haiku.assert_not_awaited()
    assert api.await_args.args == ("Be brief.", SECRET_PROMPT)
    assert api.await_args.kwargs == {"max_tokens": 8192, "timeout": 120.0}
    assert SECRET_PROMPT not in caplog.text
    assert "provider=xai" in caplog.text
    assert "unit-test-key" not in caplog.text


async def test_her_pov_json_comes_from_the_api(haiku, monkeypatch):
    monkeypatch.setenv("KLUKAI_XAI_API_KEY", "unit-test-key")
    api = AsyncMock(return_value='{"annotation": "I kept the rain."}')
    monkeypatch.setattr("app.xai_chat.complete_text", api)
    out = await bg.json_object("her_pov", "p", max_tokens=512)
    assert out == {"annotation": "I kept the rain."}
    assert api.await_args.args[0] == xl.SYSTEM_PROMPT
    haiku.assert_not_awaited()


async def test_a_capped_story_falls_back_to_the_cli(haiku, monkeypatch, caplog):
    from app.xai_chat import XaiCapError

    monkeypatch.setenv("KLUKAI_XAI_API_KEY", "unit-test-key")
    api = AsyncMock(side_effect=XaiCapError("cap"))
    monkeypatch.setattr("app.xai_chat.complete_text", api)
    with caplog.at_level(logging.INFO, logger="app.background_llm"):
        assert await bg.text("episode_summary", SECRET_PROMPT, max_tokens=150) == SECRET_REPLY
    haiku.assert_awaited()
    assert "capped->cli" in caplog.text
    assert SECRET_PROMPT not in caplog.text


async def test_a_failed_story_falls_back_to_the_cli(haiku, monkeypatch, caplog):
    from app.xai_chat import XaiChatError

    monkeypatch.setenv("KLUKAI_XAI_API_KEY", "unit-test-key")
    api = AsyncMock(side_effect=XaiChatError("xai-secret"))
    monkeypatch.setattr("app.xai_chat.complete_text", api)
    with caplog.at_level(logging.INFO, logger="app.background_llm"):
        assert await bg.json_object("her_pov", "p", max_tokens=512) is None
    haiku.assert_awaited()
    assert "failed->cli" in caplog.text
    assert "xai-secret" not in caplog.text


async def test_unusable_story_json_falls_back_to_the_cli(haiku, monkeypatch, caplog):
    monkeypatch.setenv("KLUKAI_XAI_API_KEY", "unit-test-key")
    monkeypatch.setattr("app.xai_chat.complete_text", AsyncMock(return_value="not json"))
    haiku.return_value = '{"kept": true}'
    with caplog.at_level(logging.INFO, logger="app.background_llm"):
        assert await bg.json_object("her_pov", SECRET_PROMPT, max_tokens=512) == {"kept": True}
    assert "unusable->cli" in caplog.text
    assert SECRET_PROMPT not in caplog.text


async def test_an_api_crash_falls_back_without_the_message(haiku, monkeypatch, caplog):
    monkeypatch.setenv("KLUKAI_XAI_API_KEY", "unit-test-key")
    monkeypatch.setattr(
        "app.xai_chat.complete_text", AsyncMock(side_effect=RuntimeError("xai-secret"))
    )
    with caplog.at_level(logging.WARNING, logger="app.background_llm"):
        assert await bg.text("episode_summary", "p", max_tokens=150) == SECRET_REPLY
    assert "RuntimeError" in caplog.text
    assert "xai-secret" not in caplog.text


@pytest.mark.parametrize(
    "caller",
    ["extraction", "promises", "memory_recall_episodes", "dream"],
)
async def test_other_background_callers_stay_on_the_cli(haiku, monkeypatch, caller):
    monkeypatch.setenv("KLUKAI_XAI_API_KEY", "unit-test-key")
    api = AsyncMock(return_value="from api")
    monkeypatch.setattr("app.xai_chat.complete_text", api)
    assert await bg.text(caller, "p", max_tokens=50) == SECRET_REPLY
    api.assert_not_awaited()
    haiku.assert_awaited()


async def test_an_xai_failure_logs_its_reason_without_the_key(haiku, monkeypatch, caplog):
    from app.xai_chat import XaiChatError

    monkeypatch.setenv("KLUKAI_XAI_API_KEY", "xai-secret-value-for-this-test")
    monkeypatch.setattr(
        "app.xai_chat.complete_text",
        AsyncMock(side_effect=XaiChatError("xai-secret-value-for-this-test", status=503)),
    )
    with caplog.at_level(logging.WARNING, logger="app.background_llm"):
        await bg.text("her_pov", "p", max_tokens=200)
    lines = [r.getMessage() for r in caplog.records if "xAI her_pov failed" in r.getMessage()]
    assert len(lines) == 1 and "HTTP 503" in lines[0]
    assert "xai-secret-value-for-this-test" not in caplog.text
