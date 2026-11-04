"""Chat replies through the xAI API. The CLI path stays the fallback.

Streaming is chat completions: each visible delta is text for the existing
websocket token frames. Reasoning text is not forwarded. The key comes from
``KLUKAI_XAI_API_KEY`` only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import contextvars
import json
import time
from typing import Any

import uuid

import httpx

from .circuit_breakers import CircuitOpen, get_breaker
from .xai_spend import (
    CHAT_MODEL,
    CHAT_URL,
    api_key,
    chat_estimate,
    chat_model,
    chat_usd,
    log_use,
    redact,
    ticks_usd,
    try_reserve,
    try_settle,
)

# Story and summary JSON share the CLI's background budget. Reasoning tokens
# count against it, and 2048 was cutting the object off.
STORY_COMPLETION_TOKENS = 8192
# Separate from chat so a story timeout cannot send the reply to the CLI.
STORY_BREAKER = "xai_story"

_MAX_BODY = 2_000_000
# The chat body sets no output limit; this is the reservation's ceiling.
CHAT_RESERVE_TOKENS = 4096
_REASONING_ALLOWANCE = 512
# Set by the chat reply for the whole turn. The websocket stream() signature
# stays the same, so existing callers and test doubles are unchanged.
_conversation: contextvars.ContextVar[str] = contextvars.ContextVar(
    "xai_conversation_id", default=""
)


class XaiChatError(RuntimeError):
    """A chat call that should fall back to the CLI, or stop if text already flowed."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class XaiAuthError(XaiChatError):
    """The key was rejected. This is not an outage."""


class XaiCapError(XaiChatError):
    """Today's USD cap is already spent."""


def content_delta(payload: dict[str, Any]) -> str | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    delta = choices[0].get("delta")
    if not isinstance(delta, dict):
        return None
    text = delta.get("content")
    return text if isinstance(text, str) and text else None


def usage_counts(payload: dict[str, Any]) -> tuple[int, int, int, int] | None:
    """Input, cached input, output, reasoning. Reasoning is reported, not billed twice."""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None

    def count(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return value if value > 0 else 0

    prompt_details = usage.get("prompt_tokens_details")
    completion_details = usage.get("completion_tokens_details")
    cached = 0
    reasoning = 0
    if isinstance(prompt_details, dict):
        cached = count(prompt_details.get("cached_tokens"))
    if cached == 0:
        cached = count(usage.get("cached_tokens"))
    if isinstance(completion_details, dict):
        reasoning = count(completion_details.get("reasoning_tokens"))
    if reasoning == 0:
        reasoning = count(usage.get("reasoning_tokens"))
    return (
        count(usage.get("prompt_tokens")),
        cached,
        count(usage.get("completion_tokens")),
        reasoning,
    )


def _event(line: str) -> dict[str, Any] | None:
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if data == "[DONE]":
        return {"type": "done"}
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise XaiChatError("xai chat returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise XaiChatError("xai chat returned malformed JSON")
    return payload


def cache_conversation_id(value: object) -> str | None:
    """xAI sticks a prompt cache only when ``x-grok-conv-id`` is a UUID."""
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        return None


def message_text(payload: dict[str, Any]) -> str:
    """Visible text of a non-streaming chat completion. Reasoning is not included."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    text = message.get("content")
    if not isinstance(text, str):
        return ""
    return text.strip()


def _settle(
    model: str,
    counts: tuple[int, int, int, int] | None,
    billed: float | None,
    started: float,
    first: float | None,
    *,
    hold: int | None = None,
    fallback_usd: float = 0.0,
) -> None:
    """Settle the turn's hold with the real cost, or ``fallback_usd`` when the
    reply was billed but its usage never arrived (a dropped stream)."""
    input_tokens = cached = output = reasoning = None
    if counts is not None:
        input_tokens, cached, output, reasoning = counts
        usd = chat_usd(input_tokens, cached, output) if billed is None else billed
    else:
        usd = billed if billed is not None else fallback_usd
    daily = try_settle(hold, "chat", usd)
    log_use(
        "chat",
        model=model,
        input_tokens=input_tokens,
        cached_tokens=cached,
        output_tokens=output,
        reasoning_tokens=reasoning,
        ttft_s=None if first is None else first - started,
        total_s=time.monotonic() - started,
        usd=usd,
        daily_usd=daily,
    )


def _partial_usd(prompt_chars: int, output_chars: int) -> float:
    """Cost of a reply whose usage never arrived: the prompt, the text that
    was streamed and a reasoning allowance."""
    return chat_usd(prompt_chars // 3 + 1, 0, output_chars // 3 + _REASONING_ALLOWANCE)


def conversation_messages(
    persona: str,
    turns: list[dict[str, str]],
    volatile: str = "",
) -> list[dict[str, str]]:
    """Stable persona, then real turns, then this turn's changing context.

    xAI caches a prefix of messages that match a previous request exactly.
    A changing system message before the history stops the cache at the
    persona. ``prompt_cache_key`` is a Responses API field, not chat
    completions; sticky routing stays the ``x-grok-conv-id`` header.
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": persona}]
    for turn in turns:
        role = turn.get("role")
        content = turn.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        if messages[-1]["role"] == role:
            messages[-1] = {
                "role": role,
                "content": messages[-1]["content"] + "\n" + text,
            }
            continue
        messages.append({"role": role, "content": text})
    if messages[-1]["role"] == "system":
        messages.append({"role": "user", "content": "."})
    if volatile.strip():
        messages.append({"role": "system", "content": volatile.strip()})
    return messages


async def iter_text(
    system: str,
    prompt: str,
    *,
    timeout: float = 120.0,
    conversation_id: str | None = None,
    volatile: str = "",
    turns: list[dict[str, str]] | None = None,
) -> AsyncIterator[str]:
    """Yield visible reply text. Raises before the first yield when the CLI should run."""
    key = api_key()
    if not key:
        raise XaiChatError("xai chat is not configured")
    history = turns if turns is not None else [{"role": "user", "content": prompt}]
    messages = conversation_messages(system, history, volatile)
    model = chat_model()
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "reasoning_effort": "low",
    }
    prompt_chars = len(json.dumps(messages))
    allowed, hold = try_reserve(
        "chat", chat_estimate(prompt_chars, CHAT_RESERVE_TOKENS)
    )
    if not allowed:
        raise XaiCapError("xai daily cap reached")
    started = time.monotonic()
    first: float | None = None
    counts: tuple[int, int, int, int] | None = None
    billed: float | None = None
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "text/event-stream",
    }
    # Stable per conversation so xAI can route prompt-cache hits. A label
    # that is not a UUID is not sent: the router ignores it.
    conv = cache_conversation_id(
        conversation_id if conversation_id else _conversation.get()
    )
    if conv:
        headers["x-grok-conv-id"] = conv
    yielded = False
    auth_failed = False
    # None: no reply yet (nothing billed unless the request already left).
    status: int | None = None
    sent = False
    output_chars = 0
    transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout, connect=5.0),
        ) as client:
            async with get_breaker("xai_chat").protect():
                sent = True
                async with client.stream(
                    "POST",
                    CHAT_URL,
                    json=body,
                    headers=headers,
                ) as response:
                    status = response.status_code
                    if status in {401, 403}:
                        auth_failed = True
                    elif status != 200:
                        raise XaiChatError("xai chat failed", status=status)
                    else:
                        size = 0
                        async for line in response.aiter_lines():
                            size += len(line)
                            if size > _MAX_BODY:
                                raise XaiChatError("xai chat response is too large")
                            event = _event(line)
                            if event is None:
                                continue
                            if event.get("type") == "done":
                                break
                            found = usage_counts(event)
                            if found is not None:
                                counts = found
                                usage = event.get("usage")
                                if isinstance(usage, dict):
                                    exact = ticks_usd(usage.get("cost_in_usd_ticks"))
                                    if exact is not None:
                                        billed = exact
                            piece = content_delta(event)
                            if piece is None:
                                continue
                            if first is None:
                                first = time.monotonic()
                            yielded = True
                            output_chars += len(piece)
                            yield piece
        if auth_failed:
            raise XaiAuthError("xai chat was not authorized")
        if not yielded:
            raise XaiChatError("xai chat returned no text")
    except XaiChatError:
        raise
    except CircuitOpen as exc:
        sent = False
        raise XaiChatError("xai chat is unavailable") from exc
    except (OSError, httpx.HTTPError, TimeoutError) as exc:
        raise XaiChatError(
            redact(f"xai chat is unavailable: {type(exc).__name__}")
        ) from exc
    finally:
        # Runs on a clean end, an error, and a client that went away
        # mid-reply (the generator is closed): a billed reply is always
        # recorded, from its usage when it arrived, else estimated.
        billable = status == 200 or (status is None and sent)
        _settle(
            model,
            counts,
            billed,
            started,
            first,
            hold=hold,
            fallback_usd=_partial_usd(prompt_chars, output_chars) if billable else 0.0,
        )


async def complete_text(
    system: str,
    prompt: str,
    *,
    max_tokens: int = STORY_COMPLETION_TOKENS,
    timeout: float = 120.0,
) -> str:
    """One grok-4.7 completion for a background story or summary.

    Raises before any request when the key is missing or the daily cap is
    already spent, so the caller can use the Grok CLI. The chat-model trial
    does not apply here: these calls stay on ``CHAT_MODEL``.
    """
    key = api_key()
    if not key:
        raise XaiChatError("xai chat is not configured")
    if type(max_tokens) is not int or not 1 <= max_tokens <= 8192:
        raise XaiChatError("xai chat token budget is invalid")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    body = {
        "model": CHAT_MODEL,
        "messages": messages,
        "stream": False,
        "reasoning_effort": "low",
        "max_completion_tokens": max_tokens,
    }
    prompt_chars = len(json.dumps(messages))
    allowed, hold = try_reserve("chat", chat_estimate(prompt_chars, max_tokens))
    if not allowed:
        raise XaiCapError("xai daily cap reached")
    started = time.monotonic()
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
    payload: dict[str, Any] | None = None
    status: int | None = None
    sent = False
    settled = False
    try:
        async with httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout, connect=5.0),
        ) as client:
            async with get_breaker(STORY_BREAKER).protect():
                sent = True
                response = await client.post(CHAT_URL, json=body, headers=headers)
                status = response.status_code
                if response.status_code in {401, 403}:
                    pass
                elif response.status_code != 200:
                    raise XaiChatError("xai chat failed", status=response.status_code)
                else:
                    raw = response.content
                    if len(raw) > _MAX_BODY:
                        raise XaiChatError("xai chat response is too large")
                    try:
                        parsed = json.loads(raw)
                    except ValueError as exc:
                        raise XaiChatError("xai chat returned malformed JSON") from exc
                    if not isinstance(parsed, dict) or parsed.get("error"):
                        raise XaiChatError("xai chat returned malformed JSON")
                    payload = parsed
        if payload is None:
            raise XaiAuthError("xai chat was not authorized")
        counts = usage_counts(payload)
        billed: float | None = None
        usage = payload.get("usage")
        if isinstance(usage, dict):
            exact = ticks_usd(usage.get("cost_in_usd_ticks"))
            if exact is not None:
                billed = exact
        text = message_text(payload)
        settled = True
        _settle(
            CHAT_MODEL,
            counts,
            billed,
            started,
            None,
            hold=hold,
            fallback_usd=_partial_usd(prompt_chars, max_tokens * 3),
        )
        if not text:
            raise XaiChatError("xai chat returned no text")
        return text
    except XaiChatError:
        raise
    except CircuitOpen as exc:
        sent = False
        raise XaiChatError("xai chat is unavailable") from exc
    except (OSError, httpx.HTTPError, TimeoutError) as exc:
        raise XaiChatError(
            redact(f"xai chat is unavailable: {type(exc).__name__}")
        ) from exc
    finally:
        if not settled:
            # A timeout after the request left may still be billed by xAI:
            # keep the full reservation. A refused or failed status is not.
            billable = status == 200 or (status is None and sent)
            try_settle(
                hold,
                "chat",
                chat_estimate(prompt_chars, max_tokens) if billable else 0.0,
            )
