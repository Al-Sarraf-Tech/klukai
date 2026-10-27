"""The owner-approved Haiku route: always Anthropic, key-gated, capped, credential-safe."""

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


def transport(monkeypatch, handler):
    real_client = httpx.AsyncClient
    options = []

    def client(**kwargs):
        options.append(kwargs)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(xl.httpx, "AsyncClient", client)
    return options


# ── switches ──────────────────────────────────────────────────────────────


def test_utc_clock_is_aware():
    assert xl._utc_now().tzinfo is not None


@pytest.mark.parametrize("value", [None, "local", "anthropic", "openai", ""])
def test_provider_is_always_anthropic(monkeypatch, value):
    # KLUKAI_EXTRACTION_PROVIDER is retired: whatever an old deployment still
    # exports, there is no local background model to select.
    if value is None:
        monkeypatch.delenv("KLUKAI_EXTRACTION_PROVIDER", raising=False)
    else:
        monkeypatch.setenv("KLUKAI_EXTRACTION_PROVIDER", value)
    assert xl.provider() == "anthropic"
    assert xl.uses_anthropic() is True
    assert not hasattr(xl, "PROVIDER_ENV")


def test_call_report_defaults_to_anthropic():
    report = xl.CallReport()
    assert (report.provider, report.model, report.outcome) == (
        "anthropic",
        "",
        "pending",
    )


@pytest.mark.parametrize(
    "value", ["claude-haiku-5-5", "claude-haiku-4-5", "claude-haiku-6"]
)
def test_haiku_models_are_allowed(monkeypatch, value):
    monkeypatch.setenv("KLUKAI_EXTRACTION_MODEL", value)
    assert xl.model() == value


@pytest.mark.parametrize(
    "value",
    [
        "claude-sonnet-5-5",
        "claude-opus-5-5",
        "gpt-5",
        "claude-haiku-",
        "claude-haiku-5-5 x",
        "",
    ],
)
def test_non_haiku_models_fall_back_to_the_default(monkeypatch, value):
    monkeypatch.setattr(xl, "_warned", set())
    monkeypatch.setenv("KLUKAI_EXTRACTION_MODEL", value)
    assert xl.model() == "claude-haiku-5-5"


# ── request ───────────────────────────────────────────────────────────────


async def test_request_shape_reserves_once_and_returns_visible_text(
    remote, monkeypatch
):
    reserve, record = remote
    seen = []

    def handler(request):
        assert reserve.await_count == 1  # reserved before dispatch
        seen.append(request)
        return httpx.Response(200, json=response())

    options = transport(monkeypatch, handler)
    monkeypatch.setenv("LM_STUDIO_TOKEN", "local-token-must-not-cross")
    text = await xl.complete("Classify this.", max_tokens=4096, route="extraction")
    assert text == '{"mood": "serene"}'
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "test-extraction-key"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in request.headers
    body = json.loads(request.content)
    assert body["model"] == "claude-haiku-5-5"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high"}
    assert body["max_tokens"] == 4096
    assert body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Classify this."}]}
    ]
    assert isinstance(body["system"], str) and body["system"]
    assert "temperature" not in body and "tools" not in body
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
    reserve.assert_awaited_once_with("extraction")
    record.assert_awaited_once_with(
        "attempt-1", {"input_tokens": 900, "output_tokens": 300}
    )


async def test_configured_haiku_model_is_sent(remote, monkeypatch):
    monkeypatch.setenv("KLUKAI_EXTRACTION_MODEL", "claude-haiku-4-5")
    seen = []
    transport(
        monkeypatch,
        lambda r: (
            seen.append(json.loads(r.content)) or httpx.Response(200, json=response())
        ),
    )
    await xl.complete("x", max_tokens=100, route="compaction")
    assert seen[0]["model"] == "claude-haiku-4-5"


async def test_retired_local_switch_cannot_turn_the_route_off(remote, monkeypatch):
    reserve, _ = remote
    monkeypatch.setenv("KLUKAI_EXTRACTION_PROVIDER", "local")
    seen = []
    transport(
        monkeypatch, lambda r: seen.append(r) or httpx.Response(200, json=response())
    )
    assert await xl.complete("x", max_tokens=100, route="extraction") == (
        '{"mood": "serene"}'
    )
    reserve.assert_awaited_once_with("extraction")
    assert [str(r.url) for r in seen] == [xl.MESSAGES_URL]


async def test_missing_key_never_reserves_or_dispatches(remote, monkeypatch, caplog):
    reserve, _ = remote
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    seen = []
    transport(monkeypatch, lambda r: seen.append(r))
    with caplog.at_level(logging.WARNING, logger="app.extraction_llm"):
        for _ in range(2):
            with pytest.raises(xl.ExtractionUnavailable):
                await xl.complete("x", max_tokens=100, route="extraction")
    reserve.assert_not_awaited()
    assert seen == []
    key_lines = [r.getMessage() for r in caplog.records if "API key" in r.getMessage()]
    assert key_lines == ["No Anthropic API key is set; using safe defaults"]


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
    seen = []
    transport(monkeypatch, lambda r: seen.append(r))
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


@pytest.mark.parametrize(
    "payload",
    [
        response(stop_reason="max_tokens"),
        response(stop_reason="refusal"),
        response(content=[]),
        response(content=["bare string block"]),
        response(content=[{"type": "thinking", "thinking": "", "signature": "s"}]),
        response(content=[{"type": "text", "text": "   "}]),
        response(content=[{"type": "text", "text": 4}]),
        response(
            content=[{"type": "tool_use", "id": "x", "name": "exec", "input": {}}]
        ),
        response(content=[{"type": "image", "source": {}}]),
        response(model=""),
        response(role="user"),
        response(type="error"),
        response(usage={"output_tokens": -1}),
        response(usage="bad"),
        ["not", "an", "object"],
    ],
)
async def test_malformed_or_refused_response_is_an_error(remote, monkeypatch, payload):
    reserve, record = remote
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=payload)

    transport(monkeypatch, handler)
    with pytest.raises(xl.ExtractionLLMError):
        await xl.complete("x", max_tokens=100, route="extraction")
    assert len(seen) == 1
    reserve.assert_awaited_once_with("extraction")
    record.assert_not_awaited()


@pytest.mark.parametrize("status", [302, 400, 401, 429, 500, 529])
async def test_http_failure_is_one_attempt_and_credential_free(
    remote, monkeypatch, status
):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            status,
            text="test-extraction-key private words",
            headers={"location": "https://other.invalid"},
        )

    transport(monkeypatch, handler)
    with pytest.raises(xl.ExtractionLLMError) as error:
        await xl.complete("private words", max_tokens=100, route="extraction")
    assert "private words" not in str(error.value)
    assert "test-extraction-key" not in str(error.value)
    assert len(seen) == 1


async def test_transport_error_is_wrapped(remote, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("boom")

    transport(monkeypatch, handler)
    with pytest.raises(xl.ExtractionLLMError):
        await xl.complete("x", max_tokens=100, route="extraction")


async def test_oversized_response_rejected(remote, monkeypatch):
    transport(
        monkeypatch,
        lambda r: httpx.Response(200, content=b" " * (xl._MAX_RESPONSE_BYTES + 1)),
    )
    with pytest.raises(xl.ExtractionLLMError):
        await xl.complete("x", max_tokens=100, route="extraction")


async def test_uses_its_own_breaker_not_chats(remote, monkeypatch):
    from app import dependencies

    names = []
    real = dependencies.http_stream

    def spy(dep, *args, **kwargs):
        names.append(dep)
        return real(dep, *args, **kwargs)

    monkeypatch.setattr(xl, "http_stream", spy)
    transport(monkeypatch, lambda r: httpx.Response(200, json=response()))
    await xl.complete("x", max_tokens=100, route="extraction")
    assert names == ["anthropic_extraction"]


async def test_telemetry_failure_never_fails_a_completed_call(remote, monkeypatch):
    import app.observability as obs

    monkeypatch.setattr(
        obs, "record_llm_usage", lambda **_: (_ for _ in ()).throw(RuntimeError())
    )
    transport(monkeypatch, lambda r: httpx.Response(200, json=response()))
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
