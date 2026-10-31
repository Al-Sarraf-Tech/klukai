"""The opt-in creative writer cannot bypass caps or leak local credentials."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest


def response(**overrides):
    return {
        "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
        "stop_reason": "end_turn", "content": [{"type": "text", "text": "A quiet dream."}],
        "usage": {"input_tokens": 12, "output_tokens": 5}, **overrides,
    }


@pytest.fixture
def remote(monkeypatch):
    from app import creative_text as ct
    from app import extraction_llm as xl
    monkeypatch.delenv("KLUKAI_EXTRACTION_MODEL", raising=False)
    monkeypatch.setenv("KLUKAI_CREATIVE_TEXT_PROVIDER", "anthropic")
    monkeypatch.setattr("app.grok_chat.configured", lambda: True)
    reserve = AsyncMock(return_value="reserved-attempt")
    monkeypatch.setattr(ct, "reserve_attempt", reserve)
    monkeypatch.setattr(xl, "reserve_attempt", AsyncMock(return_value="shared-haiku-attempt"))
    monkeypatch.setattr(xl, "record_usage", AsyncMock())
    return ct, reserve


def transport(monkeypatch, ct, handler):
    from app.grok_chat import GrokChatError, Turn

    seen = []

    async def complete_turn(system, prompt, *, timeout=60, role="chat"):
        seen.append({"system": system, "prompt": prompt})

        class Request:
            url = "http://klukai-agy/complete"
            headers = {}
            content = b""

        request = Request()
        response = handler(request)
        status = getattr(response, "status_code", 200)
        raw = getattr(response, "content", b"") or b""
        if status != 200 or len(raw) > 512 * 1024:
            raise GrokChatError("failed")
        try:
            body = json.loads(raw) if raw else {}
        except ValueError as exc:
            raise GrokChatError("malformed") from exc
        content = body.get("content") if isinstance(body, dict) else None
        parts = [
            block.get("text") for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"].strip()
        ] if isinstance(content, list) else []
        if not parts:
            raise GrokChatError("empty")
        return Turn("".join(parts), "grok-4.7", "medium", "stream")

    monkeypatch.setattr("app.grok_chat.complete_turn", complete_turn)
    return seen


@pytest.mark.asyncio
async def test_interactive_flag_alone_never_enables_creative_calls(monkeypatch):
    from app import creative_text as ct
    monkeypatch.delenv("KLUKAI_CREATIVE_TEXT_PROVIDER", raising=False)
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert ct.uses_cloud_text() is False
    with pytest.raises(ct.CreativeTextError):
        await ct.complete_creative_text("system", "dream")


@pytest.mark.asyncio
async def test_canonical_request_reserves_once_and_returns_only_visible_text(remote, monkeypatch):
    ct, reserve = remote
    seen = []

    def handler(request):
        from app import extraction_llm as xl
        assert reserve.await_count == 1
        xl.reserve_attempt.assert_awaited_once_with("extraction")
        seen.append(request)
        return httpx.Response(200, json=response(content=[
            {"type": "thinking", "thinking": "private reasoning", "signature": "test"},
            {"type": "text", "text": "A quiet dream."},
        ]))

    calls = transport(monkeypatch, ct, handler)
    monkeypatch.setenv("LM_STUDIO_TOKEN", "local-token-must-not-cross")
    result = await ct.complete_creative_text("You are Klukai.", "Write a dream.", max_tokens=256)
    assert result == "A quiet dream."
    assert calls == [{"system": "You are Klukai.", "prompt": "Write a dream."}]
    reserve.assert_awaited_once_with("text")


@pytest.mark.asyncio
async def test_missing_key_never_reserves_or_dispatches(remote, monkeypatch):
    ct, reserve = remote
    monkeypatch.setattr("app.grok_chat.configured", lambda: False)
    with pytest.raises(ct.CreativeTextError):
        await ct.complete_creative_text("system", "dream")
    reserve.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("system,user,tokens", [
    ("", "dream", 256), (" ", "dream", 256),
    ("s" * 8001, "dream", 256), ("system", "", 256),
    ("system", "d" * 24001, 256), ("system", "dream", 0),
    ("system", "dream", True), ("system", "dream", 8193),
])
async def test_invalid_input_never_consumes_either_allowance(remote, monkeypatch, system, user, tokens):
    ct, reserve = remote
    from app import extraction_llm as xl
    seen = []
    transport(monkeypatch, ct, lambda request: seen.append(request))
    with pytest.raises(ct.CreativeTextError):
        await ct.complete_creative_text(system, user, max_tokens=tokens)
    reserve.assert_not_awaited()
    xl.reserve_attempt.assert_not_awaited()
    assert seen == []


@pytest.mark.asyncio
async def test_success_records_usage_once_on_shared_haiku_attempt(remote, monkeypatch):
    ct, _ = remote
    from app import extraction_llm as xl
    record = AsyncMock()
    monkeypatch.setattr(xl, "record_usage", record)
    transport(monkeypatch, ct, lambda request: httpx.Response(200, json=response()))
    assert await ct.complete_creative_text("system", "dream") == "A quiet dream."
    record.assert_awaited_once_with("shared-haiku-attempt", {})


@pytest.mark.asyncio
async def test_shared_haiku_cap_also_blocks_creative_transport(remote, monkeypatch):
    ct, reserve = remote
    from app import extraction_llm as xl
    from app.creative_budget import CreativeBudgetError
    xl.reserve_attempt.side_effect = CreativeBudgetError("shared cap exhausted")
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=response())
    transport(monkeypatch, ct, handler)
    with pytest.raises(ct.CreativeTextError):
        await ct.complete_creative_text("system", "dream")
    reserve.assert_awaited_once_with("text")
    assert seen == []


@pytest.mark.asyncio
async def test_budget_denial_never_reaches_transport(remote, monkeypatch):
    ct, reserve = remote
    from app.creative_budget import CreativeBudgetError
    reserve.side_effect = CreativeBudgetError("exhausted")
    seen = []
    transport(monkeypatch, ct, lambda request: seen.append(request))
    with pytest.raises(CreativeBudgetError):
        await ct.complete_creative_text("system", "dream")
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    response(content=[]), response(content=[{"type": "text", "text": 4}]),
    response(content=[{"type": "text", "text": "   "}]),
    response(content=[{"type": "tool_use", "id": "x", "name": "exec", "input": {}}]),
    response(content=[{"type": "image", "source": {}}]),
])
async def test_malformed_or_refused_response_is_terminal(remote, monkeypatch, payload):
    ct, reserve = remote
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=payload)
    transport(monkeypatch, ct, handler)
    with pytest.raises(ct.CreativeTextError):
        await ct.complete_creative_text("system", "dream")
    assert len(seen) == 1
    reserve.assert_awaited_once_with("text")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 429, 500])
async def test_http_failure_has_no_retry_or_credential_bearing_error(remote, monkeypatch, status):
    ct, reserve = remote
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(status, text="test-creative-key private prompt", headers={"location": "https://other.invalid"})
    transport(monkeypatch, ct, handler)
    with pytest.raises(ct.CreativeTextError) as error:
        await ct.complete_creative_text("system", "private prompt")
    assert "private prompt" not in str(error.value)
    assert "test-creative-key" not in str(error.value)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_oversized_response_rejected(remote, monkeypatch):
    ct, _ = remote
    transport(monkeypatch, ct, lambda request: httpx.Response(200, content=b" " * (1024 * 1024 + 1)))
    with pytest.raises(ct.CreativeTextError):
        await ct.complete_creative_text("system", "dream")


def test_unknown_provider_is_rejected(monkeypatch):
    from app import creative_text as ct
    monkeypatch.setenv("KLUKAI_CREATIVE_TEXT_PROVIDER", "typo")
    with pytest.raises(ct.CreativeTextError):
        ct.uses_cloud_text()


@pytest.mark.parametrize("value", ["haiku", "HAIKU", "local"])
def test_haiku_and_its_old_name_keep_writing_on_the_background_route(
    monkeypatch, value
):
    from app import creative_text as ct
    monkeypatch.setenv("KLUKAI_CREATIVE_TEXT_PROVIDER", value)
    assert ct.uses_cloud_text() is False
