"""Account-login Grok for every background model call.

Owner decision 2026-10-30: background text uses the same Grok 4.7 medium
account path as chat. There is no Anthropic call and no paid fallback.

A request needs the host Grok worker and a positive
``KLUKAI_EXTRACTION_DAILY_LIMIT``. Each request first reserves one attempt in
the persistent creative ledger (kind ``extraction``: one UTC-day cap shared by
every background call). A reserved attempt stays spent after failures and
timeouts. This module raises only credential- and content-free errors;
callers fall back to safe defaults and never surface them to the chat turn.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .creative_budget import CreativeBudgetError, record_usage, reserve_attempt

logger = logging.getLogger(__name__)

MODEL_ENV = "KLUKAI_EXTRACTION_MODEL"
DEFAULT_MODEL = "grok-4.7"
EFFORT = "medium"

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
    """Always ``grok``: background text is the account-login CLI."""
    return "grok"


def uses_anthropic() -> bool:
    return False


def model() -> str:
    """Grok 4.7. A leftover Haiku id in the environment is ignored."""
    value = os.environ.get(MODEL_ENV, "").strip()
    if value and value != DEFAULT_MODEL:
        _warn_once(
            f"model:{value}",
            "%s is not the background model; using %s",
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
            "Extraction daily allowance unavailable for %s (UTC); using safe "
            "defaults until it resets",
            day,
        )


async def complete(
    prompt: str, *, max_tokens: int, route: str, system: str | None = None
) -> str:
    """Return the visible text of one budgeted Grok 4.7 medium request.

    ``system`` replaces the default background instructions, for callers that
    carry their own system prompt (her character preamble); it is sent as
    given.
    """
    from .grok_chat import EFFORT as GROK_EFFORT
    from .grok_chat import MODEL as GROK_MODEL
    from .grok_chat import GrokChatError, complete_turn, configured

    if not configured():
        _warn_once("grok", "Grok account chat is not configured; using safe defaults")
        raise ExtractionUnavailable("Grok account chat is not configured")
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or len(prompt) > MAX_PROMPT_CHARS
        or type(max_tokens) is not int
        or not 1 <= max_tokens <= MAX_REQUEST_TOKENS
        or (
            system is not None
            and (
                not isinstance(system, str)
                or not system.strip()
                or len(system) > MAX_SYSTEM_CHARS
            )
        )
    ):
        raise ExtractionLLMError("Invalid Grok extraction request")
    try:
        attempt_id = await reserve_attempt("extraction")
    except CreativeBudgetError:
        _log_cap_once()
        raise ExtractionUnavailable("Grok extraction allowance unavailable") from None
    started = time.monotonic()
    system_text = SYSTEM_PROMPT if system is None else system
    try:
        async with _gate:
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                turn = await complete_turn(system_text, prompt, timeout=REQUEST_TIMEOUT_S)
        if turn.model != GROK_MODEL or turn.effort != GROK_EFFORT:
            raise ExtractionLLMError("Unexpected Grok model or effort")
        if len(turn.text.encode()) > _MAX_RESPONSE_BYTES:
            raise ExtractionLLMError("Grok reply exceeds size limit")
        text = turn.text
    except ExtractionLLMError:
        raise
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
