"""Chat replies through the xAI API. The CLI path stays the fallback.

Streaming is chat completions: each visible delta is text for the existing
websocket token frames. Reasoning text is not forwarded. The key comes from
``KLUKAI_XAI_API_KEY`` only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import contextvars
import json
import sqlite3
import time
from typing import Any

import httpx

from .circuit_breakers import CircuitOpen, get_breaker
from .xai_spend import (
    CHAT_MODEL,
    CHAT_URL,
    api_key,
    capped,
    chat_usd,
    log_use,
    record,
    redact,
    ticks_usd,
)

_MAX_BODY = 2_000_000
# Set by the chat reply for the whole turn. The websocket stream() signature
# stays the same, so existing callers and test doubles are unchanged.
_conversation: contextvars.ContextVar[str] = contextvars.ContextVar(
    "xai_conversation_id", default=""
)


class XaiChatError(RuntimeError):
    """A chat call that should fall back to the CLI, or stop if text already flowed."""


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
    if capped():
        raise XaiCapError("xai daily cap reached")
    history = turns if turns is not None else [{"role": "user", "content": prompt}]
    messages = conversation_messages(system, history, volatile)
    body = {
        "model": CHAT_MODEL,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "reasoning_effort": "low",
    }
    started = time.monotonic()
    first: float | None = None
    counts: tuple[int, int, int, int] | None = None
    billed: float | None = None
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "text/event-stream",
    }
    # Stable per conversation so xAI can route prompt-cache hits.
    conv = conversation_id if conversation_id else _conversation.get()
    if isinstance(conv, str) and conv.strip():
        headers["x-grok-conv-id"] = conv.strip()[:128]
    yielded = False
    auth_failed = False
    transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout, connect=5.0),
        ) as client:
            async with get_breaker("xai_chat").protect():
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
                        raise XaiChatError("xai chat failed")
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
                            yield piece
        if auth_failed:
            raise XaiAuthError("xai chat was not authorized")
    except XaiChatError:
        raise
    except CircuitOpen as exc:
        raise XaiChatError("xai chat is unavailable") from exc
    except (OSError, httpx.HTTPError, TimeoutError) as exc:
        raise XaiChatError(
            redact(f"xai chat is unavailable: {type(exc).__name__}")
        ) from exc
    if not yielded:
        raise XaiChatError("xai chat returned no text")
    usd = 0.0
    daily: float | None = None
    input_tokens = cached = output = reasoning = None
    if counts is not None:
        input_tokens, cached, output, reasoning = counts
        usd = chat_usd(input_tokens, cached, output) if billed is None else billed
        try:
            daily = record("chat", usd)
        except (OSError, sqlite3.Error):
            daily = None
    log_use(
        "chat",
        model=CHAT_MODEL,
        input_tokens=input_tokens,
        cached_tokens=cached,
        output_tokens=output,
        reasoning_tokens=reasoning,
        ttft_s=None if first is None else first - started,
        total_s=time.monotonic() - started,
        usd=usd,
        daily_usd=daily,
    )
