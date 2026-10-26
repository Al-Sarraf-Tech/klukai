"""Opt-in Sonnet primary for interactive chat, with Dominus as secondary.

Background work stays local. The billable API path requires both the explicit
deployment flag and an API key; a key alone never enables it.
"""

from __future__ import annotations


import asyncio
import logging
import os
import time

import httpx

from .dependencies import http_request

from .lm_gateway import LM_TTL_SECONDS, lm_studio_auth_headers
from .models import LLMConfig as LLMConfig, SessionState
from .llm_completions import LLMCompletionsMixin

logger = logging.getLogger(__name__)

# Circuit breaker: seconds to wait before re-probing LM Studio after failure
_HEALTH_RECHECK_INTERVAL = 15.0

# ── Global LM Studio gate ─────────────────────────────────────────────────
# Single lock shared across ALL modules that call LM Studio.
# Ensures only 1 request is in-flight at a time — prevents queue pile-up
# when the server is slow or swapping models.
_lm_gate: asyncio.Lock | None = None


def get_lm_gate() -> asyncio.Lock:
    """Return the shared LM Studio request gate (created on first call)."""
    global _lm_gate
    if _lm_gate is None:
        _lm_gate = asyncio.Lock()
    return _lm_gate


def lm_gate_busy() -> bool:
    """True if an LM Studio request is currently in-flight."""
    return _lm_gate is not None and _lm_gate.locked()


LM_STUDIO_URL = os.environ.get(
    "LM_STUDIO_URL", "http://100.107.121.5:1234"
)  # Dominus RTX 3090 via Tailscale


def _lm_headers() -> dict[str, str]:
    """Required bearer header for the Tailscale-only compatibility gateway."""
    return lm_studio_auth_headers()


# This is a policy ceiling, not a tuning knob. It is intentionally independent
# of the environment so stale deployment values cannot keep an LLM resident.
MAX_LLM_IDLE_TTL_SECONDS = LM_TTL_SECONDS

# Model aliases
LOCAL_CASUAL = "cognitivecomputations_dolphin-mistral-24b-venice-edition"  # Chat: uncensored, clean streaming, no thinking tags
LOCAL_AGENT = "qwen3.5-27b-claude-4.6-opus-reasoning-distilled-v2"  # Agent: Opus-level tool-use + reasoning
LOCAL_TOOLS = LOCAL_AGENT  # Same as agent
# Gaming-mode chat persona: an abliterated Gemma-4-26B-A4B MoE model running
# --cpu-moe on the gateway side (~4-5GB VRAM vs Venice's dense ~14-18GB).
# The gateway (lmstudio-compat) is the sole source of truth for whether a
# game is active on dominus-nobara -- it reads a local marker file amarillo
# cannot see directly, so the client asks via /health instead of guessing.
# Chosen specifically as an abliterated model (not stock Gemma/Granite) so
# gaming mode doesn't reintroduce corporate-safety refusals mid-game -- the
# whole reason Venice was chosen in the first place. See
# ~/.claude/plans/klukai-handoff-node.md for the full research trail.
LOCAL_CASUAL_GAMING = "huihui-gemma-4-26b-a4b-abliterated"
# Owner authorized the billable API primary on 2026-10-14. Local background
# inference, gaming guards and model residency rules remain unchanged.
CLAUDE_PRIMARY = "claude-sonnet-5-5"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_MAX_RESPONSE_BYTES = 1024 * 1024


def claude_primary_configured() -> bool:
    return os.environ.get("KLUKAI_CLAUDE_PRIMARY", "false").lower() == "true" and bool(
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
    )


# How long a cached game-active read is trusted before re-checking /health.
# Short enough that switching in/out of gaming mode is noticed within a
# couple of chat turns; long enough not to hammer the gateway every message.
_GAME_ACTIVE_RECHECK_INTERVAL = 5.0

# Legacy activity bookkeeping remains for compatibility with older companion
# extensions and observability tests. It cannot issue requests: ``keepalive``
# below is a policy-enforced no-op and no scheduler calls it.
_KEEPALIVE_INTERVAL = 20 * 60
_model_last_used: dict[str, float] = {}
IDLE_TIMEOUT = 2 * 3600
_last_user_message: float = 0.0
_seeding_active: bool = False


def set_seeding_active(active: bool) -> None:
    global _seeding_active
    _seeding_active = active


def _is_early_am_window() -> bool:
    from datetime import datetime

    return 1 <= datetime.now().hour <= 4


def mark_user_active() -> None:
    global _last_user_message
    _last_user_message = time.monotonic()


def _is_user_idle() -> bool:
    if _last_user_message == 0.0:
        return False
    return (time.monotonic() - _last_user_message) > IDLE_TIMEOUT


def mark_model_used(model: str) -> None:
    _model_last_used[model] = time.monotonic()


def model_needs_keepalive(model: str) -> bool:
    last = _model_last_used.get(model, 0)
    return last == 0 or (time.monotonic() - last > _KEEPALIVE_INTERVAL)


# Yielded as the whole response when every backend failed BEFORE any token
# was produced. Callers key off this prefix to skip background follow-ups.
FAILURE_SENTINEL = "Communications disrupted, Commander. Standby for reconnection."


class LLMStreamFailed(Exception):
    """The LLM stream died AFTER tokens were already yielded.

    Raised instead of yielding a sentinel so callers never see a partial
    answer silently concatenated with a second full response — and so the
    sentinel prefix check can't be defeated by leading partial tokens.
    Failures before the first token can use the local secondary; if no backend
    can answer, the caller receives FAILURE_SENTINEL only.
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


class LLMRouter(LLMCompletionsMixin):
    """Routes opted-in interactive requests to Sonnet, then to Dominus."""

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._anthropic_http: httpx.AsyncClient | None = None
        self._anthropic_gate = asyncio.Semaphore(2)
        self._anthropic_retry_at = 0.0
        self._lmstudio_available: bool | None = None
        self._lmstudio_last_check: float = 0.0
        self._game_active: bool = False
        self._game_active_last_check: float = 0.0

    async def init(self) -> None:
        headers = _lm_headers()
        self._http = httpx.AsyncClient(
            timeout=60.0, headers=headers if headers else None
        )
        enabled = os.environ.get("KLUKAI_CLAUDE_PRIMARY", "false").lower() == "true"
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if claude_primary_configured():
            # Separate clients prevent the local bearer token and API key from
            # crossing provider boundaries. Never inherit endpoint/proxy overrides.
            self._anthropic_http = httpx.AsyncClient(
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                timeout=httpx.Timeout(90.0, connect=5.0),
                trust_env=False,
            )
        elif enabled:
            logger.warning("Sonnet primary enabled without an API key; using Dominus")
        await self._check_lmstudio()

    async def close(self) -> None:
        try:
            if self._http:
                await self._http.aclose()
        finally:
            if self._anthropic_http:
                await self._anthropic_http.aclose()

    def _anthropic_ready(self) -> bool:
        return (
            self._anthropic_http is not None
            and time.monotonic() >= self._anthropic_retry_at
        )

    async def _check_lmstudio(self) -> bool:
        try:
            r = await http_request(
                "lm_studio",
                self._http,
                "get",
                f"{LM_STUDIO_URL}/v1/models",
                timeout=5.0,
            )
            self._lmstudio_available = r.status_code == 200
        except httpx.HTTPError:
            self._lmstudio_available = False
        self._lmstudio_last_check = time.monotonic()
        logger.info("LM Studio available: %s", self._lmstudio_available)
        return self._lmstudio_available

    async def is_game_active(self) -> bool:
        """Read a configured host heartbeat independently of LLM availability.

        Legacy deployments without a status file retain their gateway probe.
        A missing, malformed or stale host heartbeat does not imply gaming.
        """
        from pathlib import Path

        status_file = os.environ.get("KLUKAI_GAME_STATUS_FILE", "").strip()
        default_file = "/config/.amarillo-presence.json"
        if status_file or Path(default_file).is_file():
            status_file = status_file or default_file
            from .local_presence import game_active

            return game_active(status_file)
        if self._http is None or not self._lmstudio_available:
            # No client yet, or the gateway is already known-down: don't stall
            # the caller on a doomed /health probe -- keep last-known.
            return self._game_active
        elapsed = time.monotonic() - self._game_active_last_check
        if elapsed < _GAME_ACTIVE_RECHECK_INTERVAL:
            return self._game_active
        try:
            r = await http_request(
                "lm_studio", self._http, "get", f"{LM_STUDIO_URL}/health", timeout=5.0
            )
            if r.status_code == 200:
                self._game_active = bool(r.json().get("game_active", False))
        except (httpx.HTTPError, ValueError):
            pass  # keep last-known value; gateway still enforces regardless
        self._game_active_last_check = time.monotonic()
        return self._game_active

    async def _ensure_lmstudio_fresh(self) -> bool:
        """Re-check LM Studio if it was down and enough time has passed."""
        if self._lmstudio_available:
            return True
        elapsed = time.monotonic() - self._lmstudio_last_check
        if elapsed >= _HEALTH_RECHECK_INTERVAL:
            result = await self._check_lmstudio()
            if result:
                logger.info("LM Studio recovered after %.0fs", elapsed)
            return result
        return False

    async def route(
        self,
        _message: str,
        _session: SessionState,
        needs_tools: bool = False,
        user_override: str | None = None,
        *,
        interactive: bool = False,
    ) -> LLMConfig:
        """Decide which model to use for this message."""
        if (
            interactive
            and self._anthropic_ready()
            and (not user_override or user_override == CLAUDE_PRIMARY)
        ):
            return LLMConfig(
                provider="anthropic",
                model=CLAUDE_PRIMARY,
                base_url=ANTHROPIC_MESSAGES_URL,
                max_tokens=4096,
                read_timeout=90.0,
            )

        # Re-check LM Studio if it was previously down
        if not self._lmstudio_available:
            await self._ensure_lmstudio_fresh()

        # Unconfigured cloud overrides never enable API usage implicitly.
        if user_override:
            if user_override.startswith("claude") or user_override.startswith(
                "anthropic"
            ):
                logger.warning(
                    "Ignoring unavailable cloud model override %r",
                    user_override,
                )
            else:
                return LLMConfig(
                    provider="lmstudio",
                    model=user_override,
                    base_url=LM_STUDIO_URL,
                )

        # Tool use -> local large model
        if needs_tools and self._lmstudio_available:
            return LLMConfig(
                provider="lmstudio",
                model=LOCAL_CASUAL_GAMING
                if await self.is_game_active()
                else LOCAL_TOOLS,
                base_url=LM_STUDIO_URL,
            )

        # Default: Venice for casual chat, unless dominus-nobara is mid-game --
        # then the graceful-degrade persona (LOCAL_CASUAL_GAMING) so chat
        # stays available instead of hard-failing on the gateway's 503.
        if self._lmstudio_available:
            casual_model = (
                LOCAL_CASUAL_GAMING if await self.is_game_active() else LOCAL_CASUAL
            )
            return LLMConfig(
                provider="lmstudio",
                model=casual_model,
                base_url=LM_STUDIO_URL,
            )

        # Background work and disabled/unavailable primaries need Dominus.
        raise RuntimeError("No local LLM backend available")

    async def needs_agent(self, message: str, *, interactive: bool = False) -> bool:
        """Determine if a message needs the agentic tool-use loop."""
        primary_ready = interactive and self._anthropic_ready()
        if not self._lmstudio_available and not primary_ready:
            await self._ensure_lmstudio_fresh()
        if not self._lmstudio_available and not primary_ready:
            return False

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
            "Sonnet primary unavailable (%s, HTTP %s); trying Dominus",
            type(exc).__name__,
            status,
        )

    async def _select_local_secondary(
        self, config: LLMConfig, needs_tools: bool = False
    ) -> None:
        local = await self.route(
            "", SessionState(conversation_id="fallback"), needs_tools=needs_tools
        )
        # Callers persist this config and display its model in the done frame.
        config.provider, config.model, config.base_url = (
            local.provider,
            local.model,
            local.base_url,
        )
        config.max_tokens, config.read_timeout = local.max_tokens, local.read_timeout
