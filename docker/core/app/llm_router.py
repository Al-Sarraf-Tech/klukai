"""Cloud chat through account-login Grok.

Owner decision 2026-10-30: every Klukai model call runs through the official
Grok CLI on the account login. Interactive chat and the agent loop use
grok-4.7 at medium effort. A failure stays on that path: there is no paid
Anthropic fallback. The Claude constants below remain only so older unit
tests of the unused Messages client still describe that client.
"""

from __future__ import annotations


import asyncio
import logging
import os
import time

import httpx

from .models import LLMConfig as LLMConfig, SessionState
from .llm_completions import LLMCompletionsMixin

logger = logging.getLogger(__name__)

# Owner authorized the billable API primary on 2026-10-14. It is not the chat
# path as of 2026-10-30.
CLAUDE_PRIMARY = "claude-sonnet-5-5"
CLAUDE_SECONDARY = "claude-haiku-5-5"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_MAX_RESPONSE_BYTES = 1024 * 1024
MODEL_EFFORT = {CLAUDE_PRIMARY: "medium", CLAUDE_SECONDARY: "high"}
# Pinned Grok CLI 1.0.62 accepts this id and `--effort medium`.
GROK_MODEL = "grok-4.7"
GROK_EFFORT = "medium"


def model_family(model: str) -> str | None:
    """CLAUDE_PRIMARY or CLAUDE_SECONDARY for a requested or returned model id.

    The API may answer with a dated id (``claude-haiku-5-5-20261001``); a
    config that carries it must still route to the same model next turn.
    """
    for family in MODEL_EFFORT:
        if model == family or model.startswith(f"{family}-"):
            return family
    return None


def claude_primary_configured() -> bool:
    return os.environ.get("KLUKAI_CLAUDE_PRIMARY", "false").lower() == "true" and bool(
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
    )


# Yielded as the whole response when every backend failed BEFORE any token
# was produced. Callers key off this prefix to skip background follow-ups.
FAILURE_SENTINEL = "Communications disrupted, Commander. Standby for reconnection."


class LLMStreamFailed(Exception):
    """The LLM stream died AFTER tokens were already yielded.

    Raised instead of yielding a sentinel so callers never see a partial
    answer silently concatenated with a second full response — and so the
    sentinel prefix check can't be defeated by leading partial tokens.
    Failures before the first token fall back from Sonnet to Haiku; if no
    model can answer, the caller receives FAILURE_SENTINEL only.
    """


# Signals that the message needs tool use (agent loop)
AGENT_SIGNALS = [
    "search",
    "look up",
    "find out",
    "what's happening",
    "current",
    "latest",
    "news",
    "weather",
    "browse",
    "check online",
    "what time",
    "today's",
    "right now",
    "this week",
    "who is",
    "what is the price",
    "stock",
    "score",
]


def _config(model: str) -> LLMConfig:
    return LLMConfig(
        provider="anthropic",
        model=model,
        base_url=ANTHROPIC_MESSAGES_URL,
        max_tokens=4096,
        read_timeout=90.0,
    )


def _grok_config() -> LLMConfig:
    return LLMConfig(
        provider="grok",
        model=GROK_MODEL,
        base_url="",
        max_tokens=4096,
        read_timeout=120.0,
    )


class LLMRouter(LLMCompletionsMixin):
    """Routes every turn to account-login Grok 4.7 at medium effort."""

    def __init__(self) -> None:
        self._anthropic_http: httpx.AsyncClient | None = None
        self._anthropic_gate = asyncio.Semaphore(2)
        self._anthropic_retry_at = 0.0

    async def init(self) -> None:
        # Do not open an Anthropic client. A leftover key must not bill chat.
        self._anthropic_http = None
        if os.environ.get("ANTHROPIC_API_KEY", "").strip() or (
            os.environ.get("KLUKAI_CLAUDE_PRIMARY", "false").lower() == "true"
        ):
            logger.warning("Claude is not the chat path; Grok account login is")

    async def close(self) -> None:
        if self._anthropic_http:
            await self._anthropic_http.aclose()

    def _anthropic_configured(self) -> bool:
        return self._anthropic_http is not None

    def _anthropic_ready(self) -> bool:
        """Sonnet may be tried now (configured and not backing off)."""
        return (
            self._anthropic_http is not None
            and time.monotonic() >= self._anthropic_retry_at
        )

    async def is_game_active(self) -> bool:
        """Read Amarillo's boolean heartbeat without contacting a model server."""
        from .local_presence import game_active

        path = os.environ.get("KLUKAI_GAME_STATUS_FILE", "").strip()
        return game_active(path or "/config/.amarillo-presence.json")

    async def route(
        self,
        _message: str,
        _session: SessionState,
        needs_tools: bool = False,
        user_override: str | None = None,
        *,
        interactive: bool = False,
    ) -> LLMConfig:
        """Decide which model answers this message."""
        if user_override and user_override != GROK_MODEL:
            logger.warning(
                "Ignoring model override %r; chat is grok-4.7", user_override
            )
        return _grok_config()

    async def needs_agent(self, message: str, *, interactive: bool = False) -> bool:
        """Determine if a message needs the agentic tool-use loop."""
        from .llm_intent import message_needs_agent

        return message_needs_agent(message, AGENT_SIGNALS)

    def _primary_failed(self, exc: Exception) -> None:
        delay = 60.0
        status = None
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            try:
                delay = max(
                    delay,
                    min(float(exc.response.headers.get("retry-after", "60")), 3600.0),
                )
            except ValueError:
                pass
            if status in (401, 403):
                delay = 300.0
        self._anthropic_retry_at = time.monotonic() + delay
        # Never log response bodies, prompts, request headers or credentials.
        logger.warning(
            "Sonnet primary unavailable (%s, HTTP %s); trying Haiku",
            type(exc).__name__,
            status,
        )

    def _select_secondary(self, config: LLMConfig) -> bool:
        """Switch a failed Sonnet config to Haiku; False if it already was."""
        if model_family(config.model) == CLAUDE_SECONDARY:
            return False
        secondary = _config(CLAUDE_SECONDARY)
        # Callers persist this config and display its model in the done frame.
        config.provider, config.model, config.base_url = (
            secondary.provider,
            secondary.model,
            secondary.base_url,
        )
        config.max_tokens, config.read_timeout = (
            secondary.max_tokens,
            secondary.read_timeout,
        )
        return True
