"""The two background model calls: Claude Haiku, and account-login Grok.

Owner decision 2026-11-01: the Grok account's usage ran out, so with
KLUKAI_CLAUDE_PRIMARY=true and ANTHROPIC_API_KEY set, background text is Claude
Haiku 5.5 at low effort. app/background_llm.py falls back to the Grok CLI (Grok
4.7 at low effort, owner decision 2026-10-31) when Haiku fails. Without both
settings, background text is the CLI alone and nothing is billed here.

Either request needs a positive ``KLUKAI_EXTRACTION_DAILY_LIMIT``. Each one
first reserves one attempt in the persistent creative ledger (kind
``extraction``: one UTC-day cap shared by every background call), and a reserved
attempt stays spent after failures and timeouts. This module raises only
credential- and content-free errors; callers fall back to safe defaults and
never surface them to the chat turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from .creative_budget import CreativeBudgetError, record_usage, reserve_attempt
from .dependencies import http_stream

logger = logging.getLogger(__name__)

MODEL_ENV = "KLUKAI_EXTRACTION_MODEL"
DEFAULT_MODEL = "grok-4.7"
EFFORT = "low"
CLAUDE_MODEL = "claude-haiku-5-5"
CLAUDE_EFFORT = "low"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
# Its own breaker: repeated background failures must never trip the one that
# guards chat ("anthropic"), or the other way round.
CLAUDE_BREAKER = "anthropic_extraction"
_HAIKU_ID = re.compile(r"claude-haiku-[a-z0-9]+(?:-[a-z0-9]+)*")

# Thinking counts against max_tokens; the JSON itself is a few hundred tokens.
EXTRACTION_MAX_TOKENS = 4096
COMPACTION_MAX_TOKENS = 2048
MAX_REQUEST_TOKENS = 8192
MAX_PROMPT_CHARS = 24_000
MAX_SYSTEM_CHARS = 8_000
REQUEST_TIMEOUT_S = 60.0
_MAX_RESPONSE_BYTES = 512 * 1024

SYSTEM_PROMPT = (
    "You do the background work for a character companion app: analysis, "
    "summaries and short in-character lines. Follow the instructions in the "
    "user message exactly and reply with only what they ask for: no preamble, "
    "no commentary, no markdown."
)

_warned: set[str] = set()
_cap_logged_day: str | None = None
_gate = asyncio.Semaphore(2)
_claude_gate = asyncio.Semaphore(2)


@dataclass
class CallReport:
    """Which provider and model produced a background result, and how it went.

    Filled in by app/background_llm.py, so callers (the per-turn extraction
    line in app/memory_extraction.py) can log it without ever logging content.
    """

    provider: str = "grok"
    model: str = ""
    outcome: str = "pending"


class ExtractionLLMError(RuntimeError):
    """A credential-free background failure. Callers use their safe default."""


class ExtractionCancelled(ExtractionLLMError):
    """A higher-priority recall took the background CLI. Requeue the job."""


class ExtractionUnavailable(ExtractionLLMError):
    """Not enabled, no key or no allowance left: nothing was sent."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _warn_once(key: str, message: str, *args: object) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


def uses_anthropic() -> bool:
    """Claude answers first. A key alone never opens the paid route."""
    return os.environ.get("KLUKAI_CLAUDE_PRIMARY", "false").lower() == "true" and bool(
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
    )


def provider() -> str:
    """Who answers background text first with the current settings."""
    return "anthropic" if uses_anthropic() else "grok"


def _claude_model() -> str:
    """The configured Haiku id. Any other id falls back, so it cannot be billed."""
    value = os.environ.get(MODEL_ENV, "").strip()
    if not value or _HAIKU_ID.fullmatch(value):
        return value or CLAUDE_MODEL
    _warn_once(
        f"model:{value}",
        "%s accepts only claude-haiku-* ids; using %s",
        MODEL_ENV,
        CLAUDE_MODEL,
    )
    return CLAUDE_MODEL


def model() -> str:
    """The model that answers first: Haiku, or Grok 4.7 when Claude is off."""
    return _claude_model() if uses_anthropic() else DEFAULT_MODEL


def startup_line() -> str:
    """What background text runs on, for the startup log."""
    if uses_anthropic():
        return (
            f"Background text: {_claude_model()} at {CLAUDE_EFFORT} effort, "
            f"Grok CLI as backup"
        )
    return f"Background text: {DEFAULT_MODEL} at {EFFORT} effort through the Grok CLI"


def _log_cap_once() -> None:
    global _cap_logged_day
    day = _utc_now().strftime("%Y-%m-%d")
    if _cap_logged_day != day:
        _cap_logged_day = day
        logger.warning(
            "Extraction daily allowance unavailable for %s (UTC); using safe "
            "defaults until it resets",
            day,
        )


def _valid_request(prompt: object, max_tokens: object, system: object) -> bool:
    return (
        isinstance(prompt, str)
        and bool(prompt.strip())
        and len(prompt) <= MAX_PROMPT_CHARS
        and type(max_tokens) is int
        and 1 <= max_tokens <= MAX_REQUEST_TOKENS
        and (
            system is None
            or (
                isinstance(system, str)
                and bool(system.strip())
                and len(system) <= MAX_SYSTEM_CHARS
            )
        )
    )


def _time_budget(role: str, timeout: float | None) -> float:
    """The caller's timeout, else the recall or the standard background budget."""
    from .memory_settings import RECALL_SELECTION_TIMEOUT_S

    if timeout is None or timeout <= 0:
        return RECALL_SELECTION_TIMEOUT_S if role == "recall" else REQUEST_TIMEOUT_S
    return timeout


def _visible_text(data: object) -> tuple[str, str, dict[str, Any]]:
    """Validate a finished, unrefused reply; return (text, model, usage)."""
    if (
        not isinstance(data, dict)
        or data.get("type") != "message"
        or data.get("role") != "assistant"
        or data.get("stop_reason") != "end_turn"
        or not isinstance(data.get("model"), str)
        or not data["model"].strip()
        or not isinstance(data.get("content"), list)
    ):
        raise ExtractionLLMError("Haiku reply is incomplete or refused")
    parts: list[str] = []
    for block in data["content"]:
        if not isinstance(block, dict):
            raise ExtractionLLMError("Malformed Haiku content")
        kind = block.get("type")
        if kind == "text":
            if not isinstance(block.get("text"), str):
                raise ExtractionLLMError("Malformed Haiku text")
            parts.append(block["text"])
        elif kind not in ("thinking", "redacted_thinking"):
            raise ExtractionLLMError("Unexpected Haiku content")
    text = "".join(parts).strip()
    if not text:
        raise ExtractionLLMError("Empty Haiku reply")
    usage = data.get("usage", {})
    if not isinstance(usage, dict) or any(
        field in usage and (type(usage[field]) is not int or usage[field] < 0)
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    ):
        raise ExtractionLLMError("Malformed Haiku usage")
    return text, data["model"], usage


async def complete_claude(
    prompt: str,
    *,
    max_tokens: int,
    route: str,
    system: str | None = None,
    role: str = "background",
    timeout: float | None = None,
) -> str:
    """Return the visible text of one budgeted, paid Haiku request."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise ExtractionUnavailable("Haiku has no API key")
    if not _valid_request(prompt, max_tokens, system):
        raise ExtractionLLMError("Invalid Haiku request")
    body = {
        "model": _claude_model(),
        "system": SYSTEM_PROMPT if system is None else system,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        "max_tokens": max_tokens,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": CLAUDE_EFFORT},
    }
    try:
        attempt_id = await reserve_attempt("extraction")
    except CreativeBudgetError:
        _log_cap_once()
        raise ExtractionUnavailable("Haiku allowance unavailable") from None
    started = time.monotonic()
    budget = _time_budget(role, timeout)
    try:
        async with _claude_gate:
            async with asyncio.timeout(budget):
                # A dedicated client: canonical endpoint, no redirects or proxy.
                async with httpx.AsyncClient(
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                    timeout=httpx.Timeout(budget, connect=5.0),
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    async with http_stream(
                        CLAUDE_BREAKER, client, "POST", MESSAGES_URL, json=body
                    ) as result:
                        result.raise_for_status()
                        raw = bytearray()
                        async for chunk in result.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > _MAX_RESPONSE_BYTES:
                                raise ExtractionLLMError(
                                    "Haiku reply exceeds size limit"
                                )
        text, used_model, usage = _visible_text(json.loads(raw))
    except ExtractionLLMError:
        raise
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError) as error:
        status = (
            error.response.status_code
            if isinstance(error, httpx.HTTPStatusError)
            else None
        )
        # Never expose a response body, the prompt, or request headers.
        raise ExtractionLLMError(
            f"Haiku request failed ({type(error).__name__}, HTTP {status})"
        ) from None

    await record_usage(attempt_id, usage)
    try:
        from .observability import record_llm_usage

        record_llm_usage(
            model=used_model,
            tokens_in=sum(
                usage.get(field, 0)
                for field in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            ),
            tokens_out=usage.get("output_tokens", 0),
            latency_ms=(time.monotonic() - started) * 1000,
            route=route,
        )
    except Exception:
        pass  # telemetry cannot retry an already completed paid request
    return text


async def complete(
    prompt: str,
    *,
    max_tokens: int,
    route: str,
    system: str | None = None,
    role: str = "background",
    timeout: float | None = None,
) -> str:
    """Return the visible text of one budgeted Grok 4.7 request on the CLI.

    ``system`` replaces the default background instructions, for callers that
    carry their own system prompt (her character preamble); it is sent as
    given.
    """
    from .grok_chat import MODEL as GROK_MODEL
    from .grok_chat import (
        GrokChatCancelled,
        GrokChatError,
        background_effort,
        complete_turn,
        configured,
    )

    if not configured():
        _warn_once("grok", "Grok account chat is not configured; using safe defaults")
        raise ExtractionUnavailable("Grok account chat is not configured")
    if not _valid_request(prompt, max_tokens, system):
        raise ExtractionLLMError("Invalid Grok extraction request")
    try:
        attempt_id = await reserve_attempt("extraction")
    except CreativeBudgetError:
        _log_cap_once()
        raise ExtractionUnavailable("Grok extraction allowance unavailable") from None
    started = time.monotonic()
    system_text = SYSTEM_PROMPT if system is None else system
    budget = _time_budget(role, timeout)
    try:
        async with _gate:
            async with asyncio.timeout(budget):
                turn = await complete_turn(
                    system_text, prompt, timeout=budget, role=role
                )
        if turn.model != GROK_MODEL or turn.effort != background_effort():
            raise ExtractionLLMError("Unexpected Grok model or effort")
        if len(turn.text.encode()) > _MAX_RESPONSE_BYTES:
            raise ExtractionLLMError("Grok reply exceeds size limit")
        text = turn.text
    except ExtractionLLMError:
        raise
    except GrokChatCancelled:
        raise ExtractionCancelled("cancelled") from None
    except (GrokChatError, TimeoutError, ValueError, TypeError) as error:
        raise ExtractionLLMError(
            f"Grok request failed ({type(error).__name__})"
        ) from None

    await record_usage(attempt_id, {})
    try:
        from .observability import record_llm_usage

        record_llm_usage(
            model=GROK_MODEL,
            tokens_in=0,
            tokens_out=0,
            latency_ms=(time.monotonic() - started) * 1000,
            route=route,
        )
    except Exception:
        pass  # telemetry cannot retry an already completed request
    return text
