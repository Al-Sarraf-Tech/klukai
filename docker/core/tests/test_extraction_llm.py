"""Background text is account-login Grok 4.7 at low effort, capped and credential-safe."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from app import extraction_llm as xl


def response(**overrides):
    return {
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "stop_reason": "end_turn",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": '{"mood": "serene"}'},
        ],
        "usage": {"input_tokens": 900, "output_tokens": 300},
        **overrides,
    }


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.delenv("KLUKAI_EXTRACTION_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-extraction-key")
    reserve = AsyncMock(return_value="attempt-1")
    monkeypatch.setattr(xl, "reserve_attempt", reserve)
    record = AsyncMock()
    monkeypatch.setattr(xl, "record_usage", record)
    monkeypatch.setattr(xl, "_warned", set())
    monkeypatch.setattr(xl, "_cap_logged_day", None)
    return reserve, record


def transport(monkeypatch, handler=None):
    from app.grok_chat import GrokChatError, Turn

    seen = []
    monkeypatch.setattr("app.grok_chat.configured", lambda: True)

    async def complete_turn(system, prompt, *, timeout=60, role="chat"):
        seen.append({"system": system, "prompt": prompt, "model": "grok-4.7", "effort": "low"})
        if handler is not None:
            outcome = handler(system, prompt)
            if isinstance(outcome, Exception):
                raise outcome
            if isinstance(outcome, Turn):
                return outcome
        return Turn('{"mood": "serene"}', "grok-4.7", "low", "stream")

    monkeypatch.setattr("app.grok_chat.complete_turn", complete_turn)
    return seen


# ── switches ──────────────────────────────────────────────────────────────


def test_utc_clock_is_aware():
    assert xl._utc_now().tzinfo is not None


@pytest.mark.parametrize("value", [None, "local", "anthropic", "openai", ""])
def test_provider_is_always_grok(monkeypatch, value):
    # Whatever an old deployment still exports, background text is Grok.
    if value is None:
        monkeypatch.delenv("KLUKAI_EXTRACTION_PROVIDER", raising=False)
    else:
        monkeypatch.setenv("KLUKAI_EXTRACTION_PROVIDER", value)
    assert xl.provider() == "grok"
    assert xl.uses_anthropic() is False
    assert not hasattr(xl, "PROVIDER_ENV")


def test_call_report_defaults_to_grok():
    report = xl.CallReport()
    assert (report.provider, report.model, report.outcome) == (
        "grok",
        "",
        "pending",
    )


@pytest.mark.parametrize(
    "value", ["claude-haiku-5-5", "claude-sonnet-5-5", "gpt-5", ""]
)
def test_leftover_model_ids_stay_on_grok_4_7(monkeypatch, value):
    monkeypatch.setattr(xl, "_warned", set())
    if value:
        monkeypatch.setenv("KLUKAI_EXTRACTION_MODEL", value)
    else:
        monkeypatch.delenv("KLUKAI_EXTRACTION_MODEL", raising=False)
    assert xl.model() == "grok-4.7"


# ── request ───────────────────────────────────────────────────────────────


async def test_request_shape_reserves_once_and_returns_visible_text(
    remote, monkeypatch
):
    reserve, record = remote

    def handler(system, prompt):
        assert reserve.await_count == 1  # reserved before dispatch
        assert prompt == "Classify this."
        assert system == xl.SYSTEM_PROMPT
        return None

    seen = transport(monkeypatch, handler)
    monkeypatch.setenv("LM_STUDIO_TOKEN", "local-token-must-not-cross")
    text = await xl.complete("Classify this.", max_tokens=4096, route="extraction")
    assert text == '{"mood": "serene"}'
    assert seen[0]["model"] == "grok-4.7" and seen[0]["effort"] == "low"
    assert "local-token" not in json.dumps(seen)
    reserve.assert_awaited_once_with("extraction")
    record.assert_awaited_once_with("attempt-1", {})


async def test_leftover_haiku_model_is_not_sent(remote, monkeypatch):
    monkeypatch.setenv("KLUKAI_EXTRACTION_MODEL", "claude-haiku-4-5")
    seen = transport(monkeypatch)
    await xl.complete("x", max_tokens=100, route="compaction")
    assert seen[0]["model"] == "grok-4.7"


async def test_retired_local_switch_cannot_turn_the_route_off(remote, monkeypatch):
    reserve, _ = remote
    monkeypatch.setenv("KLUKAI_EXTRACTION_PROVIDER", "local")
    seen = transport(monkeypatch)
    assert await xl.complete("x", max_tokens=100, route="extraction") == (
        '{"mood": "serene"}'
    )
    reserve.assert_awaited_once_with("extraction")
    assert seen[0]["model"] == "grok-4.7"


async def test_unconfigured_grok_never_reserves_or_dispatches(remote, monkeypatch, caplog):
    reserve, _ = remote
    monkeypatch.setattr("app.grok_chat.configured", lambda: False)
    seen = []
    monkeypatch.setattr(
        "app.grok_chat.complete_turn",
        lambda *a, **k: seen.append(a) or (_ for _ in ()).throw(AssertionError("sent")),
    )
    with caplog.at_level(logging.WARNING, logger="app.extraction_llm"):
        for _ in range(2):
            with pytest.raises(xl.ExtractionUnavailable):
                await xl.complete("x", max_tokens=100, route="extraction")
    reserve.assert_not_awaited()
    assert seen == []
    lines = [r.getMessage() for r in caplog.records if "not configured" in r.getMessage()]
    assert lines == ["Grok account chat is not configured; using safe defaults"]


@pytest.mark.parametrize(
    ("prompt", "max_tokens"),
    [
        ("", 100),
        ("   ", 100),
        ("x", 0),
        ("x", 9000),
        ("x" * (xl.MAX_PROMPT_CHARS + 1), 100),
    ],
)
async def test_invalid_requests_never_reserve(remote, prompt, max_tokens):
    reserve, _ = remote
    with pytest.raises(xl.ExtractionLLMError):
        await xl.complete(prompt, max_tokens=max_tokens, route="extraction")
    reserve.assert_not_awaited()


async def test_cap_denial_never_dispatches_and_logs_once_per_day(
    remote, monkeypatch, caplog
):
    reserve, _ = remote
    from app.creative_budget import CreativeBudgetError

    reserve.side_effect = CreativeBudgetError("exhausted")
    seen = transport(monkeypatch)
    day = {"now": datetime(2026, 10, 19, 23, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(xl, "_utc_now", lambda: day["now"])
    with caplog.at_level(logging.WARNING, logger="app.extraction_llm"):
        for _ in range(3):
            with pytest.raises(xl.ExtractionUnavailable):
                await xl.complete("x", max_tokens=100, route="extraction")
        day["now"] = datetime(2026, 10, 20, 0, 5, tzinfo=timezone.utc)
        with pytest.raises(xl.ExtractionUnavailable):
            await xl.complete("x", max_tokens=100, route="compaction")
    assert seen == []
    cap_lines = [r for r in caplog.records if "allowance" in r.getMessage()]
    assert len(cap_lines) == 2  # once on the 19th, once on the 20th


async def test_wrong_effort_is_an_error(remote, monkeypatch):
    from app.grok_chat import Turn

    reserve, record = remote
    transport(monkeypatch, lambda system, prompt: Turn("x", "grok-4.7", "high", "stream"))
    with pytest.raises(xl.ExtractionLLMError, match="effort"):
        await xl.complete("x", max_tokens=100, route="extraction")
    reserve.assert_awaited_once_with("extraction")
    record.assert_not_awaited()


async def test_grok_failure_is_one_attempt_and_credential_free(remote, monkeypatch):
    from app.grok_chat import GrokChatError

    seen = transport(
        monkeypatch,
        lambda system, prompt: GrokChatError("test-extraction-key private words"),
    )
    with pytest.raises(xl.ExtractionLLMError) as error:
        await xl.complete("private words", max_tokens=100, route="extraction")
    assert "private words" not in str(error.value)
    assert "test-extraction-key" not in str(error.value)
    assert len(seen) == 1


async def test_oversized_response_rejected(remote, monkeypatch):
    from app.grok_chat import Turn

    transport(
        monkeypatch,
        lambda system, prompt: Turn("x" * (xl._MAX_RESPONSE_BYTES + 1), "grok-4.7", "low", "stream"),
    )
    with pytest.raises(xl.ExtractionLLMError, match="size"):
        await xl.complete("x", max_tokens=100, route="extraction")


async def test_background_call_does_not_open_the_anthropic_breaker(remote, monkeypatch):
    transport(monkeypatch)
    assert await xl.complete("x", max_tokens=100, route="extraction") == '{"mood": "serene"}'
    assert not hasattr(xl, "http_stream")


async def test_telemetry_failure_never_fails_a_completed_call(remote, monkeypatch):
    import app.observability as obs

    monkeypatch.setattr(
        obs, "record_llm_usage", lambda **_: (_ for _ in ()).throw(RuntimeError())
    )
    transport(monkeypatch)
    assert (
        await xl.complete("x", max_tokens=100, route="extraction")
        == '{"mood": "serene"}'
    )


# ── shared daily allowance ────────────────────────────────────────────────


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    from app import creative_budget as budget

    path = tmp_path / "private" / "attempts.sqlite3"
    monkeypatch.setenv("KLUKAI_CREATIVE_LEDGER", str(path))
    monkeypatch.setattr(
        budget, "_utc_now", lambda: datetime(2026, 10, 19, tzinfo=timezone.utc)
    )
    return budget, path


@pytest.mark.parametrize("value", ["0", "-2", "", "lots", "1.5"])
async def test_extraction_allowance_defaults_to_no_paid_calls(
    ledger, monkeypatch, value
):
    budget, path = ledger
    monkeypatch.setenv("KLUKAI_EXTRACTION_DAILY_LIMIT", value)
    with pytest.raises(budget.CreativeBudgetError):
        await budget.reserve_attempt("extraction")
    assert not path.exists()


async def test_unset_extraction_allowance_denies(ledger, monkeypatch):
    budget, path = ledger
    monkeypatch.delenv("KLUKAI_EXTRACTION_DAILY_LIMIT", raising=False)
    with pytest.raises(budget.CreativeBudgetError):
        await budget.reserve_attempt("extraction")
    assert not path.exists()


async def test_extraction_cap_is_daily_and_separate_from_creative(ledger, monkeypatch):
    budget, _ = ledger
    monkeypatch.setenv("KLUKAI_EXTRACTION_DAILY_LIMIT", "2")
    assert await budget.reserve_attempt("extraction")
    assert await budget.reserve_attempt("extraction")
    with pytest.raises(budget.CreativeBudgetError):
        await budget.reserve_attempt("extraction")
    # Creative text keeps its own (disabled) allowance.
    with pytest.raises(budget.CreativeBudgetError):
        await budget.reserve_attempt("text")
    # The next UTC day starts fresh: no monthly cap applies to extraction.
    monkeypatch.setattr(
        budget, "_utc_now", lambda: datetime(2026, 10, 20, tzinfo=timezone.utc)
    )
    assert await budget.reserve_attempt("extraction")
