"""The one background-model route: Haiku, then Dominus while it is up, else defaults."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from app import background_llm as bg
from app import extraction_llm as xl
from app.circuit_breakers import get_breaker

SECRET_PROMPT = "the Commander's private words"
SECRET_REPLY = "her private reply"


@pytest.fixture
def breaker():
    b = get_breaker("lm_studio")
    b.force_close()
    yield b
    b.force_close()


@pytest.fixture
def haiku(monkeypatch, breaker):
    monkeypatch.setenv("KLUKAI_EXTRACTION_PROVIDER", "anthropic")
    monkeypatch.delenv("KLUKAI_EXTRACTION_MODEL", raising=False)
    complete = AsyncMock(return_value=SECRET_REPLY)
    monkeypatch.setattr(xl, "complete", complete)
    return complete


@pytest.fixture
def local_only(monkeypatch, breaker):
    monkeypatch.setenv("KLUKAI_EXTRACTION_PROVIDER", "local")
    complete = AsyncMock(side_effect=AssertionError("Haiku must not be called"))
    monkeypatch.setattr(xl, "complete", complete)
    return complete


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


def test_local_circuit_closed_tracks_the_dominus_breaker(breaker):
    assert bg.local_circuit_closed() is True
    breaker.force_open()
    assert bg.local_circuit_closed() is False


# ── local provider: unchanged behaviour ──────────────────────────────────


async def test_local_provider_runs_only_the_local_request(local_only, caplog):
    local = AsyncMock(return_value=SECRET_REPLY)
    with caplog.at_level(logging.INFO, logger="app.background_llm"):
        out = await bg.text("dream", SECRET_PROMPT, max_tokens=200, local=local)
    assert out == SECRET_REPLY
    local.assert_awaited_once()
    local_only.assert_not_awaited()
    assert _lines(caplog) == [
        f"Background model: caller=dream provider=local model={bg.LOCAL_MODEL} "
        "outcome=ok"
    ]


async def test_local_provider_ignores_the_circuit(local_only, breaker):
    # The local path is exactly what it was: no new circuit gate in front of it.
    breaker.force_open()
    local = AsyncMock(return_value="line")
    assert await bg.text("dream", "p", max_tokens=200, local=local) == "line"


@pytest.mark.parametrize("value", ["", "   ", None, 7])
async def test_local_provider_unusable_text_is_empty(local_only, caplog, value):
    report = xl.CallReport()
    with caplog.at_level(logging.INFO, logger="app.background_llm"):
        out = await bg.text(
            "romance",
            "p",
            max_tokens=200,
            local=AsyncMock(return_value=value),
            report=report,
        )
    assert out == ""
    assert report.outcome == "defaults"
    assert _lines(caplog)[0].endswith("outcome=defaults")


async def test_local_failure_never_raises(local_only):
    local = AsyncMock(side_effect=RuntimeError("Dominus is off"))
    assert await bg.text("dream", "p", max_tokens=200, local=local) == ""
    assert await bg.json_object("promises", "p", max_tokens=640, local=local) is None


@pytest.mark.parametrize("value", [{}, [], ["a"], "text", None])
async def test_local_provider_json_needs_a_non_empty_object(local_only, value):
    out = await bg.json_object(
        "promises", "p", max_tokens=640, local=AsyncMock(return_value=value)
    )
    assert out is None


async def test_local_provider_json_object_passes_through(local_only):
    out = await bg.json_object(
        "promises", "p", max_tokens=640, local=AsyncMock(return_value={"a": 1})
    )
    assert out == {"a": 1}


# ── anthropic provider ───────────────────────────────────────────────────


async def test_haiku_answers_without_touching_dominus(haiku, caplog):
    local = AsyncMock(side_effect=AssertionError("Dominus must not be called"))
    report = xl.CallReport()
    with caplog.at_level(logging.INFO, logger="app.background_llm"):
        out = await bg.text(
            "mission_update", SECRET_PROMPT, max_tokens=150, local=local, report=report
        )
    assert out == SECRET_REPLY
    haiku.assert_awaited_once_with(
        SECRET_PROMPT,
        max_tokens=150 + bg.THINKING_HEADROOM_TOKENS,
        route="mission_update",
    )
    assert (report.provider, report.model, report.outcome) == (
        "anthropic",
        "claude-haiku-5-5",
        "ok",
    )
    lines = _lines(caplog)
    assert lines == [
        "Background model: caller=mission_update provider=anthropic "
        "model=claude-haiku-5-5 outcome=ok"
    ]
    # Content never reaches any log line from this module.
    for record in caplog.records:
        assert SECRET_PROMPT not in record.getMessage()
        assert SECRET_REPLY not in record.getMessage()


async def test_system_prompt_and_explicit_budget_are_forwarded(haiku):
    await bg.text(
        "weekly_reflection",
        "write",
        system="You are Klukai.",
        max_tokens=400,
        haiku_max_tokens=1234,
        local=AsyncMock(),
    )
    haiku.assert_awaited_once_with(
        "write", max_tokens=1234, route="weekly_reflection", system="You are Klukai."
    )


@pytest.mark.parametrize(
    ("effect", "reason"),
    [
        (xl.ExtractionUnavailable("cap"), "unavailable"),
        (xl.ExtractionLLMError("boom"), "failed"),
        (RuntimeError("unexpected"), "failed"),
    ],
)
async def test_haiku_problem_falls_back_to_dominus_while_its_circuit_is_closed(
    haiku, effect, reason, caplog
):
    haiku.side_effect = effect
    local = AsyncMock(return_value="local line")
    report = xl.CallReport()
    with caplog.at_level(logging.INFO, logger="app.background_llm"):
        out = await bg.text("dream", "p", max_tokens=200, local=local, report=report)
    assert out == "local line"
    assert (report.provider, report.model) == ("local", bg.LOCAL_MODEL)
    assert report.outcome == f"{reason}->local"
    assert _lines(caplog)[-1].endswith(f"outcome={reason}->local")


async def test_blank_haiku_text_counts_as_empty(haiku):
    haiku.return_value = "   "
    report = xl.CallReport()
    out = await bg.text(
        "dream", "p", max_tokens=200, local=AsyncMock(return_value="x"), report=report
    )
    assert out == "x"
    assert report.outcome == "empty->local"


async def test_haiku_problem_with_dominus_down_returns_the_default(haiku, breaker):
    haiku.side_effect = xl.ExtractionUnavailable("cap")
    breaker.force_open()
    local = AsyncMock(side_effect=AssertionError("circuit is open"))
    report = xl.CallReport()
    assert await bg.text("dream", "p", max_tokens=200, local=local, report=report) == ""
    assert await bg.json_object("promises", "p", max_tokens=640, local=local) is None
    local.assert_not_awaited()
    assert report.provider == "anthropic"
    assert report.outcome == "unavailable->defaults"


async def test_local_fallback_failure_is_defaults(haiku):
    haiku.side_effect = xl.ExtractionLLMError("boom")
    report = xl.CallReport()
    out = await bg.text(
        "dream",
        "p",
        max_tokens=200,
        local=AsyncMock(side_effect=RuntimeError("down")),
        report=report,
    )
    assert out == ""
    assert report.outcome == "failed->defaults"


@pytest.mark.parametrize(
    "reply",
    [
        '{"promises": [], "events": [{"what": "x"}]}',
        'Sure:\n```json\n{"promises": [], "events": [{"what": "x"}]}\n```',
        'Here you go {"promises": [], "events": [{"what": "x"}]} done',
    ],
)
async def test_haiku_json_is_parsed_tolerantly(haiku, reply):
    haiku.return_value = reply
    out = await bg.json_object("promises", "p", max_tokens=640, local=AsyncMock())
    assert out == {"promises": [], "events": [{"what": "x"}]}


@pytest.mark.parametrize("reply", ["no json here", "[1, 2]", "{}"])
async def test_haiku_json_without_an_object_falls_back(haiku, reply):
    haiku.return_value = reply
    report = xl.CallReport()
    out = await bg.json_object(
        "promises",
        "p",
        max_tokens=640,
        local=AsyncMock(return_value={"promises": []}),
        report=report,
    )
    assert out == {"promises": []}
    assert report.outcome == "unparsable->local"


async def test_report_is_optional(haiku):
    assert await bg.text("dream", "p", max_tokens=10, local=AsyncMock()) == SECRET_REPLY


# ── transport: the system override ───────────────────────────────────────


def _reply():
    return {
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "Welcome back, Commander."}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


@pytest.fixture
def wire(monkeypatch):
    monkeypatch.setenv("KLUKAI_EXTRACTION_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-background-key")
    reserve = AsyncMock(return_value="attempt-1")
    monkeypatch.setattr(xl, "reserve_attempt", reserve)
    monkeypatch.setattr(xl, "record_usage", AsyncMock())
    seen: list[dict] = []
    real_client = httpx.AsyncClient

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_reply())

    monkeypatch.setattr(
        xl.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
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
    assert seen[0]["thinking"] == {"type": "adaptive"}
    assert seen[0]["output_config"] == {"effort": "high"}
    reserve.assert_awaited_once_with("extraction")


@pytest.mark.parametrize("system", ["", "   ", "s" * (xl.MAX_SYSTEM_CHARS + 1), 5])
async def test_invalid_system_never_reserves(wire, system):
    reserve, seen = wire
    with pytest.raises(xl.ExtractionLLMError):
        await xl.complete("x", max_tokens=100, route="dream", system=system)
    reserve.assert_not_awaited()
    assert seen == []


# ── one cap for every caller ─────────────────────────────────────────────


async def test_every_caller_spends_the_same_daily_allowance(
    monkeypatch, tmp_path, breaker
):
    """Two attempts allowed: the third caller of any kind gets its default."""
    from app import creative_budget as budget

    monkeypatch.setenv("KLUKAI_EXTRACTION_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-background-key")
    monkeypatch.setenv("KLUKAI_EXTRACTION_DAILY_LIMIT", "2")
    monkeypatch.setenv("KLUKAI_CREATIVE_LEDGER", str(tmp_path / "l" / "a.sqlite3"))
    monkeypatch.setattr(
        budget, "_utc_now", lambda: datetime(2026, 10, 19, tzinfo=timezone.utc)
    )
    monkeypatch.setattr(xl, "_cap_logged_day", None)
    sent = []
    real_client = httpx.AsyncClient

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json=_reply())

    monkeypatch.setattr(
        xl.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    breaker.force_open()  # Dominus off
    local = AsyncMock(side_effect=AssertionError("circuit is open"))

    assert await bg.text("dream", "p", max_tokens=200, local=local)
    assert await bg.text("memory_recall", "p", max_tokens=160, local=local)
    assert await bg.text("mission_update", "p", max_tokens=150, local=local) == ""
    assert len(sent) == 2
