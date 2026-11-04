"""Streamed Claude Haiku chat replies, with a chat-pace breaker and the cap.

The visible text of a Messages API stream is forwarded as it arrives; thinking
deltas never are. The request is cache-friendly: the persona is one cached
system block, the conversation follows it with a cache breakpoint on the last
turn before his new line, and the turn's changing context rides in that new
user message. Nothing that changes every turn sits ahead of the history.

Failure policy (owner, 2026-11-04): one or two bad turns move chat to Grok.
The ``anthropic`` breaker opens after two failures in five minutes, a 401/403
backs Claude off for five minutes, and a 429/529 for its ``retry-after``. A
reply must start streaming events within ``FIRST_EVENT_S``, show text within
``FIRST_TEXT_S`` and finish within ``TOTAL_S``.

Spend: every request reserves its ceiling in the shared daily ledger before
it is sent (``xai_spend.reserve``) and settles the real usage afterwards, also
when the reply was cut off. A spent cap raises before anything is sent.
Never logged: prompts, replies, headers, keys or response bodies.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import json
import logging
import os
import time
from typing import Any

import httpx

from .circuit_breakers import CircuitOpen, get_breaker

logger = logging.getLogger(__name__)

BREAKER = "anthropic"
PERSONA_CACHE = {"type": "ephemeral", "ttl": "1h"}
HISTORY_CACHE = {"type": "ephemeral"}
CONTEXT_LABEL = "[Context for this reply. The Commander did not write this.]"
GATE_WAIT_S = 5.0
_MAX_BODY = 2_000_000
_OUTPUT_ALLOWANCE = 1024

_retry_at = 0.0


class AnthropicChatError(ValueError):
    """Claude did not answer this turn. Before any text, chat moves to Grok."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AnthropicCapError(AnthropicChatError):
    """Today's shared USD cap is spent; nothing was sent."""


def _seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def first_event_s() -> float:
    return _seconds("KLUKAI_CLAUDE_FIRST_EVENT_S", 10.0)


def first_text_s() -> float:
    return _seconds("KLUKAI_CLAUDE_FIRST_TEXT_S", 30.0)


def total_s() -> float:
    return _seconds("KLUKAI_CLAUDE_TOTAL_S", 90.0)


def backing_off() -> bool:
    return time.monotonic() < _retry_at


def reset_backoff() -> None:
    global _retry_at
    _retry_at = 0.0


def note_status(status: int | None, retry_after: str | None) -> None:
    """Back off after a key problem or an overload, never after a 5xx alone."""
    global _retry_at
    delay = 0.0
    if status in (401, 403):
        delay = 300.0
    elif status in (429, 529):
        delay = 30.0
        try:
            delay = max(delay, min(float(retry_after or "30"), 600.0))
        except ValueError:
            pass
    if delay:
        _retry_at = max(_retry_at, time.monotonic() + delay)
        logger.warning("Claude chat backing off %ds (HTTP %s)", int(delay), status)


def chat_body(
    *,
    model: str,
    effort: str,
    persona: str,
    turns: list[dict[str, str]],
    volatile: str,
    max_tokens: int,
) -> dict[str, Any]:
    """The cache-friendly request: persona, history, then this turn."""
    messages: list[dict[str, Any]] = []
    for turn in turns:
        role, text = turn.get("role"), turn.get("content")
        if role not in ("user", "assistant") or not isinstance(text, str):
            continue
        text = text.strip()
        if not text:
            continue
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"][0]["text"] += "\n" + text
        else:
            messages.append({"role": role, "content": [{"type": "text", "text": text}]})
    if not messages or messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": [{"type": "text", "text": "."}]})
    if messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": [{"type": "text", "text": "."}]})
    if len(messages) > 1:
        # The last turn before his new line is the same bytes next turn.
        messages[-2]["content"][-1]["cache_control"] = dict(HISTORY_CACHE)
    if volatile.strip():
        final = messages[-1]["content"]
        final.insert(
            0, {"type": "text", "text": f"{CONTEXT_LABEL}\n{volatile.strip()}"}
        )
    return {
        "model": model,
        "system": [
            {"type": "text", "text": persona, "cache_control": dict(PERSONA_CACHE)}
        ],
        "messages": messages,
        "max_tokens": max_tokens,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
        "stream": True,
    }


def _event(line: str) -> dict[str, Any] | None:
    if not line.startswith("data:"):
        return None
    try:
        payload = json.loads(line[5:].strip())
    except ValueError as exc:
        raise AnthropicChatError("Claude stream was malformed") from exc
    if not isinstance(payload, dict):
        raise AnthropicChatError("Claude stream was malformed")
    return payload


def _merge_usage(usage: dict[str, Any], found: object) -> None:
    if not isinstance(found, dict):
        return
    for key, value in found.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            usage[key] = value
        elif key == "cache_creation" and isinstance(value, dict):
            usage[key] = dict(value)


async def stream_text(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, Any],
    gate: asyncio.Semaphore,
    *,
    route: str = "chat",
    on_model: Callable[[str], None] | None = None,
) -> AsyncIterator[str]:
    """Yield visible reply text. Raises AnthropicChatError before any text
    when Grok should answer instead; after text, any error ends the turn."""
    from .xai_spend import anthropic_estimate, anthropic_usd, try_reserve, try_settle

    if backing_off():
        raise AnthropicChatError("Claude chat is backing off")
    prompt_chars = len(json.dumps(body["messages"])) + len(json.dumps(body["system"]))
    max_tokens = int(body["max_tokens"])
    allowed, hold = try_reserve(
        "anthropic", anthropic_estimate(prompt_chars, max_tokens)
    )
    if not allowed:
        raise AnthropicCapError("daily API cap reached")
    started = time.monotonic()
    first: float | None = None
    usage: dict[str, Any] = {}
    status: int | None = None
    sent = False
    output_chars = 0
    model = str(body["model"])
    acquired = False
    finished = False
    try:
        try:
            await asyncio.wait_for(gate.acquire(), GATE_WAIT_S)
        except TimeoutError as exc:
            raise AnthropicChatError("Claude chat is busy") from exc
        acquired = True
        deadline = started + total_s()
        try:
            async with get_breaker(BREAKER).protect():
                sent = True
                request = client.build_request(
                    "POST",
                    url,
                    json=body,
                    timeout=httpx.Timeout(total_s(), connect=5.0),
                )
                response = await asyncio.wait_for(
                    client.send(request, stream=True), first_event_s()
                )
                try:
                    status = response.status_code
                    if status != 200:
                        note_status(status, response.headers.get("retry-after"))
                        raise AnthropicChatError("Claude chat failed", status=status)
                    lines = response.aiter_lines()
                    size = 0
                    stop: str | None = None
                    while True:
                        now = time.monotonic()
                        if first is None:
                            limit = (
                                started + first_text_s()
                                if usage
                                else started + first_event_s()
                            )
                        else:
                            limit = deadline
                        remaining = min(limit, deadline) - now
                        if remaining <= 0:
                            raise TimeoutError("Claude chat timed out")
                        try:
                            line = await asyncio.wait_for(anext(lines), remaining)
                        except StopAsyncIteration:
                            break
                        size += len(line)
                        if size > _MAX_BODY:
                            raise AnthropicChatError("Claude stream is too large")
                        event = _event(line)
                        if event is None:
                            continue
                        kind = event.get("type")
                        if kind == "message_start":
                            message = event.get("message")
                            if isinstance(message, dict):
                                _merge_usage(usage, message.get("usage"))
                                if isinstance(message.get("model"), str):
                                    model = message["model"] or model
                                    if on_model is not None:
                                        on_model(model)
                            usage.setdefault("input_tokens", 0)
                        elif kind == "content_block_delta":
                            delta = event.get("delta")
                            if (
                                isinstance(delta, dict)
                                and delta.get("type") == "text_delta"
                                and isinstance(delta.get("text"), str)
                                and delta["text"]
                            ):
                                if first is None:
                                    first = time.monotonic()
                                output_chars += len(delta["text"])
                                yield delta["text"]
                        elif kind == "message_delta":
                            _merge_usage(usage, event.get("usage"))
                            finished = True
                            delta = event.get("delta")
                            if isinstance(delta, dict) and isinstance(
                                delta.get("stop_reason"), str
                            ):
                                stop = delta["stop_reason"]
                        elif kind == "message_stop":
                            break
                        elif kind == "error":
                            raise AnthropicChatError("Claude stream reported an error")
                    if first is None:
                        if stop == "refusal":
                            first = time.monotonic()
                            yield "I can't help with that request."
                        else:
                            raise AnthropicChatError("Claude returned no text")
                finally:
                    await response.aclose()
        except CircuitOpen as exc:
            sent = False
            raise AnthropicChatError("Claude chat breaker is open") from exc
        except (httpx.HTTPError, OSError) as exc:
            raise AnthropicChatError(
                f"Claude chat failed ({type(exc).__name__})"
            ) from exc
    finally:
        if acquired:
            gate.release()
        billable = status == 200 or (status is None and sent)
        if usage:
            priced = dict(usage)
            if not finished:
                # Cut off before the final usage: count what was streamed
                # plus a thinking allowance.
                priced["output_tokens"] = max(
                    int(priced.get("output_tokens", 0) or 0),
                    output_chars // 3 + _OUTPUT_ALLOWANCE,
                )
            usd = anthropic_usd(priced)
        elif billable:
            usd = anthropic_estimate(
                prompt_chars, output_chars // 3 + _OUTPUT_ALLOWANCE
            )
        else:
            usd = 0.0
        daily = try_settle(hold, "anthropic", usd)
        _log(model, usage, started, first, usd, daily, route)


def _log(
    model: str,
    usage: dict[str, Any],
    started: float,
    first: float | None,
    usd: float,
    daily: float | None,
    route: str,
) -> None:
    total_ms = (time.monotonic() - started) * 1000
    logger.info(
        "Claude chat: model=%s ttft_ms=%s total_ms=%d input=%d cache_read=%d "
        "cache_write=%d output=%d usd=%.6f daily_usd=%s",
        model,
        "-" if first is None else str(int((first - started) * 1000)),
        total_ms,
        int(usage.get("input_tokens", 0) or 0),
        int(usage.get("cache_read_input_tokens", 0) or 0),
        int(usage.get("cache_creation_input_tokens", 0) or 0),
        int(usage.get("output_tokens", 0) or 0),
        usd,
        "-" if daily is None else f"{daily:.6f}",
    )
    try:
        from .observability import record_llm_usage

        record_llm_usage(
            model=model,
            tokens_in=sum(
                int(usage.get(key, 0) or 0)
                for key in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            ),
            tokens_out=int(usage.get("output_tokens", 0) or 0),
            latency_ms=total_ms,
            route=route,
        )
    except Exception:
        pass  # telemetry cannot retry a finished paid request
