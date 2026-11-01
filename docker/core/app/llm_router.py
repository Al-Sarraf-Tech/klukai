"""Cloud chat: Claude Haiku 5.5 at xhigh effort, with Grok as the fallback.

Owner decision 2026-10-31: "set chat model to haiku xhigh". With
KLUKAI_CLAUDE_PRIMARY=true and ANTHROPIC_API_KEY set, interactive chat and the
agent loop use claude-haiku-5-5 at effort xhigh with adaptive thinking. A
Haiku failure before any text falls back to the Grok chat path (the xAI API,
then the account-login CLI). Without both settings, chat is Grok as decided on
2026-10-30. Background text is not routed here.
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

# Owner authorized the billable API primary on 2026-10-14. Chat is Haiku at
# xhigh as of 2026-10-31; Sonnet is no longer selected.
CLAUDE_PRIMARY = "claude-sonnet-5-5"
CLAUDE_SECONDARY = "claude-haiku-5-5"
CHAT_MODEL = CLAUDE_SECONDARY
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_MAX_RESPONSE_BYTES = 1024 * 1024
MODEL_EFFORT = {CLAUDE_PRIMARY: "medium", CLAUDE_SECONDARY: "xhigh"}
# Adaptive thinking at xhigh counts toward max_tokens, so leave room for it.
CHAT_MAX_TOKENS = 16000
# Pinned Grok CLI 1.0.62 accepts this id. Chat and background default to low;
# pictures use xhigh. KLUKAI_GROK_CHAT_EFFORT overrides the chat default.
GROK_MODEL = "grok-4.7"
GROK_EFFORT = "low"


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


def chat_provider() -> tuple[str, str]:
    """(provider, model) that answers chat with the current settings."""
    if claude_primary_configured():
        return "anthropic", CHAT_MODEL
    return "grok", GROK_MODEL


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
        max_tokens=CHAT_MAX_TOKENS,
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
    """Routes every turn to Haiku at xhigh when Claude is on, else to Grok."""

    def __init__(self) -> None:
        self._anthropic_http: httpx.AsyncClient | None = None
        self._anthropic_gate = asyncio.Semaphore(2)
        self._anthropic_retry_at = 0.0

    async def init(self) -> None:
        # A key alone never opens the paid client: the switch must be on too.
        self._anthropic_http = None
        enabled = os.environ.get("KLUKAI_CLAUDE_PRIMARY", "false").lower() == "true"
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if claude_primary_configured():
            # Never inherit endpoint/proxy overrides.
            self._anthropic_http = httpx.AsyncClient(
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                timeout=httpx.Timeout(90.0, connect=5.0),
                trust_env=False,
            )
            logger.info("Chat is %s at effort %s", CHAT_MODEL, MODEL_EFFORT[CHAT_MODEL])
        elif enabled:
            logger.warning(
                "KLUKAI_CLAUDE_PRIMARY is on without ANTHROPIC_API_KEY; chat is Grok"
            )
        elif key:
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
        model = CHAT_MODEL if self._anthropic_configured() else GROK_MODEL
        if (
            user_override
            and model_family(user_override) != model
            and (user_override != model)
        ):
            logger.warning(
                "Ignoring model override %r; chat is %s", user_override, model
            )
        if model == CHAT_MODEL:
            return _config(CHAT_MODEL)
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

    def _select_grok(self, config: LLMConfig) -> None:
        """Move a failed Claude config onto the Grok chat path."""
        grok = _grok_config()
        # Callers persist this config and display its model in the done frame.
        config.provider, config.model, config.base_url = (
            grok.provider,
            grok.model,
            grok.base_url,
        )
        config.max_tokens, config.read_timeout = grok.max_tokens, grok.read_timeout

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
