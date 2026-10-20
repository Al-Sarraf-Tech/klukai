"""Owner-approved Claude Haiku route for per-turn extraction and compaction.

Owner decision, 2026-10-19: the per-turn background extraction (mood,
interaction and affection, intensity for her heart rate, facts) and the
session compaction summaries may run on Claude Haiku at high effort, on the
Anthropic key interactive chat already uses. Nothing else moves here: dreams,
Her POV, memory-archive annotation, seeding, image prompts, episode journal
entries and promise detection keep their own providers.

A paid request needs both explicit switches: ``KLUKAI_EXTRACTION_PROVIDER=
anthropic`` and a positive ``KLUKAI_EXTRACTION_DAILY_LIMIT``. Each request
first reserves one attempt in the persistent creative ledger (kind
``extraction``: one UTC-day cap shared by extraction and compaction). A
reserved attempt stays spent after failures and timeouts. This module raises
only credential- and content-free errors; callers fall back to the local
model or to safe defaults and never surface them to the chat turn.
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

PROVIDER_ENV = "KLUKAI_EXTRACTION_PROVIDER"
MODEL_ENV = "KLUKAI_EXTRACTION_MODEL"
DEFAULT_MODEL = "claude-haiku-5-5"
EFFORT = "high"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
# Its own breaker: repeated extraction failures must never trip the breaker
# that guards interactive chat's Sonnet primary ("anthropic"), or vice versa.
BREAKER = "anthropic_extraction"

# Thinking counts against max_tokens; the JSON itself is a few hundred tokens.
EXTRACTION_MAX_TOKENS = 4096
COMPACTION_MAX_TOKENS = 2048
MAX_PROMPT_CHARS = 24_000
REQUEST_TIMEOUT_S = 60.0
_MAX_RESPONSE_BYTES = 512 * 1024
_MODEL_ID = re.compile(r"claude-haiku-[a-z0-9]+(?:-[a-z0-9]+)*")

SYSTEM_PROMPT = (
    "You are the background analyst for a character companion app. Follow the "
    "instructions in the user message exactly and reply with only what they ask "
    "for: no preamble, no commentary, no markdown."
)

_warned: set[str] = set()
_cap_logged_day: str | None = None
_gate = asyncio.Semaphore(2)


@dataclass
class CallReport:
    """Which provider and model produced a background result, and how it went.

    Filled in by the caller-facing functions in app/fact_extractor.py so the
    turn can log one line per extraction without ever logging content.
    """

    provider: str = "local"
    model: str = ""
    outcome: str = "pending"


class ExtractionLLMError(RuntimeError):
    """A terminal, credential-free failure of one Haiku request."""


class ExtractionUnavailable(ExtractionLLMError):
    """Not enabled, no key or no allowance left: nothing was sent."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _warn_once(key: str, message: str, *args: object) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


def provider() -> str:
    """``local`` (default) or ``anthropic``; anything else stays local."""
    value = os.environ.get(PROVIDER_ENV, "local").strip().lower()
    if value in {"local", "anthropic"}:
        return value
    _warn_once(f"provider:{value}", "Unknown %s; using local", PROVIDER_ENV)
    return "local"


def uses_anthropic() -> bool:
    return provider() == "anthropic"


def model() -> str:
    """The configured Haiku id; any non-Haiku id falls back to the default."""
    value = os.environ.get(MODEL_ENV, "").strip()
    if not value:
        return DEFAULT_MODEL
    if _MODEL_ID.fullmatch(value):
        return value
    _warn_once(
        f"model:{value}",
        "%s accepts only claude-haiku-* ids; using %s",
        MODEL_ENV,
        DEFAULT_MODEL,
    )
    return DEFAULT_MODEL


def _log_cap_once() -> None:
    global _cap_logged_day
    day = _utc_now().strftime("%Y-%m-%d")
    if _cap_logged_day != day:
        _cap_logged_day = day
        logger.warning(
            "Extraction daily allowance unavailable for %s (UTC); using the local "
            "model or safe defaults until it resets",
            day,
        )


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


async def complete(prompt: str, *, max_tokens: int, route: str) -> str:
    """Return the visible text of one authorized, budgeted Haiku request."""
    if not uses_anthropic():
        raise ExtractionUnavailable("Haiku extraction is not enabled")
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        _warn_once(
            "key", "%s=anthropic but no API key is set; using local", PROVIDER_ENV
        )
        raise ExtractionUnavailable("Haiku extraction has no API key")
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or len(prompt) > MAX_PROMPT_CHARS
        or type(max_tokens) is not int
        or not 1 <= max_tokens <= 8192
    ):
        raise ExtractionLLMError("Invalid Haiku extraction request")
    body = {
        "model": model(),
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        "max_tokens": max_tokens,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": EFFORT},
    }
    try:
        attempt_id = await reserve_attempt("extraction")
    except CreativeBudgetError:
        _log_cap_once()
        raise ExtractionUnavailable("Haiku extraction allowance unavailable") from None
    started = time.monotonic()
    try:
        async with _gate:
            # A dedicated client keeps the local bearer token on its side of the
            # provider boundary. Canonical endpoint; no redirects or ambient proxy.
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                async with httpx.AsyncClient(
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                    timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=5.0),
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    async with http_stream(
                        BREAKER, client, "POST", MESSAGES_URL, json=body
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
