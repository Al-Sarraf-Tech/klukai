"""Opt-in Sonnet primary for interactive chat, with Dominus as secondary.

Background work stays local. The billable API path requires both the explicit
deployment flag and an API key; a key alone never enables it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator

import httpx

from .anthropic_messages import completion_message, local_messages, messages_payload, tool_definitions
from .lm_gateway import LM_TTL_SECONDS, lm_studio_auth_headers
from .models import LLMConfig, SessionState

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

LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://100.107.121.5:1234")  # Dominus RTX 3090 via Tailscale

def _lm_headers() -> dict[str, str]:
    """Required bearer header for the Tailscale-only compatibility gateway."""
    return lm_studio_auth_headers()

# This is a policy ceiling, not a tuning knob. It is intentionally independent
# of the environment so stale deployment values cannot keep an LLM resident.
MAX_LLM_IDLE_TTL_SECONDS = LM_TTL_SECONDS

# Model aliases
LOCAL_CASUAL = "cognitivecomputations_dolphin-mistral-24b-venice-edition"  # Chat: uncensored, clean streaming, no thinking tags
LOCAL_AGENT = "qwen3.5-27b-claude-4.6-opus-reasoning-distilled-v2"     # Agent: Opus-level tool-use + reasoning
LOCAL_TOOLS = LOCAL_AGENT                                               # Same as agent
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
    return (os.environ.get("KLUKAI_CLAUDE_PRIMARY", "false").lower() == "true"
            and bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()))

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
    "search", "look up", "find out", "what's happening", "current",
    "latest", "news", "weather", "browse", "check online",
    "what time", "today's", "right now", "this week",
    "who is", "what is the price", "stock", "score",
]


class LLMRouter:
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
        self._http = httpx.AsyncClient(timeout=60.0, headers=headers if headers else None)
        enabled = os.environ.get("KLUKAI_CLAUDE_PRIMARY", "false").lower() == "true"
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if claude_primary_configured():
            # Separate clients prevent the local bearer token and API key from
            # crossing provider boundaries. Never inherit endpoint/proxy overrides.
            self._anthropic_http = httpx.AsyncClient(
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                timeout=httpx.Timeout(90.0, connect=5.0), trust_env=False,
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
        return self._anthropic_http is not None and time.monotonic() >= self._anthropic_retry_at

    async def _check_lmstudio(self) -> bool:
        try:
            r = await self._http.get(f"{LM_STUDIO_URL}/v1/models", timeout=5.0)
            self._lmstudio_available = r.status_code == 200
        except httpx.HTTPError:
            self._lmstudio_available = False
        self._lmstudio_last_check = time.monotonic()
        logger.info("LM Studio available: %s", self._lmstudio_available)
        return self._lmstudio_available

    async def is_game_active(self) -> bool:
        """Ask the gateway (the source of truth) whether a game currently
        owns dominus-nobara's GPU. Cached briefly -- amarillo cannot read
        dominus's local marker file directly, so this is a network check,
        not a filesystem one. Fails closed (assumes NOT gaming, i.e. keeps
        Venice) on a network error, matching this router's existing
        fail-open-to-local-only-then-fail-closed-on-total-loss posture --
        the gateway itself is the actual enforcement point and will still
        503 a blocked model regardless of what this cache believes.
        """
        if self._http is None or not self._lmstudio_available:
            # No client yet, or the gateway is already known-down: don't stall
            # the caller on a doomed /health probe -- keep last-known.
            return self._game_active
        elapsed = time.monotonic() - self._game_active_last_check
        if elapsed < _GAME_ACTIVE_RECHECK_INTERVAL:
            return self._game_active
        try:
            r = await self._http.get(f"{LM_STUDIO_URL}/health", timeout=5.0)
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
        if interactive and self._anthropic_ready() and (
            not user_override or user_override == CLAUDE_PRIMARY
        ):
            return LLMConfig(provider="anthropic", model=CLAUDE_PRIMARY,
                             base_url=ANTHROPIC_MESSAGES_URL, max_tokens=4096,
                             read_timeout=90.0)

        # Re-check LM Studio if it was previously down
        if not self._lmstudio_available:
            await self._ensure_lmstudio_fresh()

        # Unconfigured cloud overrides never enable API usage implicitly.
        if user_override:
            if user_override.startswith("claude") or user_override.startswith("anthropic"):
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
                model=LOCAL_CASUAL_GAMING if await self.is_game_active() else LOCAL_TOOLS,
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
        import re

        primary_ready = interactive and self._anthropic_ready()
        if not self._lmstudio_available and not primary_ready:
            await self._ensure_lmstudio_fresh()
        if not self._lmstudio_available and not primary_ready:
            return False

        lower = message.casefold()

        # Explicit public-web requests take priority over conversational forms
        # such as "could you". No classifier inference is needed.
        if re.search(r"https?://\S+|\bwww\.\S+", lower):
            return True
        if (re.search(r"\b(?:web|internet|online|brave)\b", lower)
                and re.search(r"\b(?:search|check|browse|look|read|find|use)\b", lower)):
            return True

        # Possessive subjects identify her state/world; freshness words do not
        # make "your latest dream" a public-information request. A definite
        # subject qualified by a destination or organization ("the mission to
        # Europa", "the team at OpenAI") is not enough to establish roleplay.
        if re.search(
            r"\b(?:your|our)\s+(?:(?:first|next|last|current|new|latest)\s+)?"
            r"(?:sector|hq|base|camp|squad|mission|team|weapon|memories|dreams?|mood|feelings|outfit)\b"
            r"|\bthe\s+(?:(?:first|next|last|current|new|latest)\s+)?"
            r"(?:sector|hq|base|camp|squad|mission|team|weapon|memories)\b"
            r"(?!\s+(?:at|to|of|for|in)\s+(?!(?:your|our|my)\b)\w)"
            r"|\b(?:t-doll|squad leader)\b", lower,
        ):
            return False

        if re.search(r"\b(?:search|look\s+up|find\s+out|research|browse|check\s+online)\b", lower):
            return True

        if re.search(r"\b(?:do|would|could) you (?:like|prefer|enjoy|hate)\b", lower):
            return False

        requested = "?" in message or bool(re.search(
            r"\b(?:can|could|would|please|tell|show|give|find|check|convert|calculate|summarize)\b"
            r"|^\s*(?:what|who|where|when|how|read)\b", lower,
        ))
        if requested and re.search(
            r"\b(?:weather|forecast|rain|snow|temperature|humidity|news|headlines|current|latest|"
            r"prices?|stocks?|scores?|exchange\s+rates?|"
            r"currency|arxiv|doi|papers?|documentation|docs|references?|wikipedia|"
            r"encyclop(?:ae|e)dia|stack\s*(?:overflow|exchange))\b", lower,
        ):
            return True
        if requested and re.search(
            r"\b(?:who is|where is|when did|how many people|how much does|what time)\s+"
            r"(?!you\b|your\b|our\b|my\b|klukai\b|commander\b)", lower,
        ):
            return True

        # Numeric conversion requests need the public units/currency tools,
        # including forms such as "how many pounds is 5 kg?".
        if requested and re.search(r"\d", lower) and re.search(
            r"\b(?:convert|usd|eur|gbp|jpy|cad|aud|chf|cny|inr|dollars?|euros?|yen|"
            r"pounds?|miles?|kilometers?|kilometres?|meters?|metres?|feet|inches|"
            r"kg|lbs?|kilograms?|grams?|ounces?|liters?|litres?|gallons?|celsius|fahrenheit)\b",
            lower,
        ):
            return True

        # Preserve compact external prompts ("latest weather") and familiar
        # factual questions without turning a casual weather remark into work.
        rp_context = any(re.search(rf"\b{re.escape(w)}\b", lower) for w in [
            "you", "your", "klukai", "commander", "feel", "think",
            "opinion", "favorite", "like", "hate", "they", "them",
            "squad", "mission", "team", "soldier", "t-doll",
            "remember", "discover", "did we", "did she", "did he",
            "would", "could", "should", "shall",
            "base", "hq", "camp", "sector", "area", "weapon",
        ])

        if not rp_context and (requested or len(lower.split()) <= 4) and any(
            re.search(rf"\b{re.escape(signal)}\b", lower) for signal in AGENT_SIGNALS
        ):
            return True

        # Question marks: only trigger agent if the question is clearly about
        # real-world external info, NOT in-character RP or conversational questions.
        if "?" in message and not rp_context and any(
            w in lower for w in ["who is", "what is the price", "how much does",
                                  "how many people", "where is", "when did"]
        ):
            return True

        return False

    async def stream(
        self, system_prompt: str, messages: list[dict], config: LLMConfig
    ) -> AsyncIterator[str]:
        """Answer using the selected primary, then stream the local secondary.

        LM Studio calls go through a global gate (1-at-a-time) to prevent
        queue pile-up when the server is slow or swapping models.
        The API answer is buffered and validated before yielding anything, so
        a failed primary can fall back without concatenating two answers.

        Failure semantics: a failure BEFORE any token yields FAILURE_SENTINEL
        as the whole response. A failure AFTER tokens were yielded raises
        LLMStreamFailed so a partial answer is never silently concatenated
        with a second full response.
        """
        yielded = False

        if config.provider == "anthropic" and self._anthropic_http is not None:
            try:
                response = await self._complete_anthropic(system_prompt, messages, config)
                yield response["choices"][0]["message"]["content"]
                return
            except (httpx.HTTPError, ValueError, TimeoutError) as exc:
                self._primary_failed(exc)
                try:
                    await self._select_local_secondary(config)
                except RuntimeError:
                    yield FAILURE_SENTINEL
                    return

        if config.provider != "lmstudio":
            # A hand-built config cannot enable an unconfigured API backend.
            logger.error(
                "Refusing unavailable LLM provider %r",
                config.provider,
            )
            yield FAILURE_SENTINEL
            return

        # ── LM Studio path: acquire gate so only 1 request is in-flight ──
        gate = get_lm_gate()
        async with gate:
            try:
                async for token in self._stream_lmstudio(
                    system_prompt, messages, config
                ):
                    yielded = True
                    yield token
                mark_model_used(config.model)
                return
            except httpx.ReadTimeout:
                logger.warning(
                    "LLM %s/%s ReadTimeout — server overloaded, skipping local retries",
                    config.provider, config.model,
                )
                self._lmstudio_available = False
                self._lmstudio_last_check = time.monotonic()
                local_error: Exception | None = None
            except httpx.HTTPStatusError as e:
                # The gateway answered, so it is up. A 503 is the game-mode
                # block on this model; a 400/500 is this request (context
                # overflow, bad body). Neither is a reason to take the whole
                # gateway down for 15s. Drop the game-active cache so the next
                # route() re-reads /health and picks the gaming model if a game
                # just started.
                logger.warning(
                    "LLM %s/%s HTTP %s — gateway stays available",
                    config.provider, config.model, e.response.status_code,
                )
                self._game_active_last_check = 0.0
                local_error = e
            except Exception as e:
                error_msg = str(e) or type(e).__name__
                logger.warning(
                    "LLM %s/%s failed: %s — skipping local retries",
                    config.provider, config.model, error_msg,
                )
                self._lmstudio_available = False
                self._lmstudio_last_check = time.monotonic()
                local_error = e
        # ── Gate released ──

        if yielded:
            # Mid-stream death after partial local tokens: surface the failure
            # rather than inventing a second answer.
            raise LLMStreamFailed(
                "local stream died mid-response"
            ) from local_error

        # The local secondary is the final backend.
        yield FAILURE_SENTINEL

    def _primary_failed(self, exc: Exception) -> None:
        delay = 60.0
        status = None
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            try:
                delay = max(delay, min(float(exc.response.headers.get("retry-after", "60")), 3600.0))
            except ValueError:
                pass
            if status in (401, 403):
                delay = 300.0
        self._anthropic_retry_at = time.monotonic() + delay
        # Never log response bodies, prompts, request headers or credentials.
        logger.warning("Sonnet primary unavailable (%s, HTTP %s); trying Dominus",
                       type(exc).__name__, status)

    async def _select_local_secondary(self, config: LLMConfig, needs_tools: bool = False) -> None:
        local = await self.route("", SessionState(conversation_id="fallback"), needs_tools=needs_tools)
        # Callers persist this config and display its model in the done frame.
        config.provider, config.model, config.base_url = local.provider, local.model, local.base_url
        config.max_tokens, config.read_timeout = local.max_tokens, local.read_timeout

    async def _complete_anthropic(
        self, system_prompt: str, messages: list[dict], config: LLMConfig,
        tools: list[dict] | None = None,
        *, allow_tools: bool = True,
    ) -> dict:
        if not self._anthropic_ready():
            raise ValueError("Anthropic primary is unavailable")
        system, turns = messages_payload(system_prompt, messages)
        body = {"model": CLAUDE_PRIMARY, "system": system, "messages": turns,
                "max_tokens": min(max(config.max_tokens, 1), 8192),
                "thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}}
        if tools:
            body["tools"] = tool_definitions(tools)
            if not allow_tools:
                body["tool_choice"] = {"type": "none"}
        start = time.monotonic()
        # Bound both queueing and HTTP lifetime. Cancellation propagates rather
        # than starting a second inference after the websocket disconnects.
        async with asyncio.timeout(min(config.read_timeout, 90.0)):
            async with self._anthropic_gate:
                async with self._anthropic_http.stream("POST", ANTHROPIC_MESSAGES_URL, json=body) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > _ANTHROPIC_MAX_RESPONSE_BYTES:
                            raise ValueError("Anthropic response exceeds size limit")
        data = json.loads(raw)
        message = completion_message(data, tools if allow_tools else None)
        if not isinstance(data.get("model"), str) or not data["model"]:
            raise ValueError("Missing response model")
        config.model = data["model"]
        usage = data.get("usage") or {}
        try:
            from .observability import record_llm_usage
            record_llm_usage(
                model=config.model,
                tokens_in=sum(int(usage.get(key, 0) or 0) for key in
                              ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
                tokens_out=int(usage.get("output_tokens", 0) or 0),
                latency_ms=(time.monotonic() - start) * 1000, route="chat",
            )
        except Exception:
            pass
        return {"model": config.model, "choices": [{"message": message}], "usage": usage}

    async def complete_chat(
        self, system_prompt: str, messages: list[dict], config: LLMConfig,
        tools: list[dict] | None = None,
        *, allow_tools: bool = True,
    ) -> dict:
        """Interactive tool-loop completion with a local secondary."""
        if config.provider == "anthropic" and self._anthropic_http is not None:
            try:
                return await self._complete_anthropic(system_prompt, messages, config, tools,
                                                       allow_tools=allow_tools)
            except (httpx.HTTPError, ValueError, TimeoutError) as exc:
                self._primary_failed(exc)
                await self._select_local_secondary(config, needs_tools=bool(tools))
        if config.provider != "lmstudio":
            raise ValueError("Unavailable LLM provider")
        return await self.complete_local(system_prompt, messages, config, tools if allow_tools else None)

    async def complete_local(
        self,
        system_prompt: str,
        messages: list[dict],
        config: LLMConfig,
        tools: list[dict] | None = None,
    ) -> dict:
        """Non-streaming completion via LM Studio with optional tool-use.

        Gated: only 1 LM Studio request at a time.

        Returns an OpenAI-compatible response dict with:
        - choices[0].message.content (text)
        - choices[0].message.tool_calls (list of tool calls, if any)
        """
        if config.provider != "lmstudio":
            raise ValueError("complete_local requires a local provider")
        gate = get_lm_gate()
        async with gate:
            oai_messages = [{"role": "system", "content": system_prompt}] + local_messages(messages)

            body: dict = {
                "model": config.model,
                "messages": oai_messages,
                "max_tokens": config.max_tokens,
                "temperature": config.temperature,
                "stream": False,
                "ttl": LM_TTL_SECONDS,
            }
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"

            import time as _time
            _start = _time.monotonic()
            r = await self._http.post(
                f"{config.base_url}/v1/chat/completions",
                json=body,
                timeout=120.0,
            )
            r.raise_for_status()
            data = r.json()
            try:
                from .observability import record_llm_usage
                usage = data.get("usage") or {}
                record_llm_usage(
                    model=config.model,
                    tokens_in=int(usage.get("prompt_tokens", 0) or 0),
                    tokens_out=int(usage.get("completion_tokens", 0) or 0),
                    latency_ms=(_time.monotonic() - _start) * 1000,
                    route="chat",
                )
            except Exception:
                pass
            return data


    async def keepalive(self) -> None:
        """Compatibility no-op: strict policy forbids extending LLM residency."""
        logger.warning("LLM keepalive ignored: strict max idle TTL is %ss", LM_TTL_SECONDS)

    async def _stream_lmstudio(
        self, system_prompt: str, messages: list[dict], config: LLMConfig
    ) -> AsyncIterator[str]:
        oai_messages = [{"role": "system", "content": system_prompt}] + local_messages(messages)

        # Some models (e.g. dolphin-mistral-glm thinking variants) emit all tokens
        # under reasoning_content and leave content empty.  We collect reasoning
        # tokens as a fallback and yield them if no content tokens appear at all.
        reasoning_parts: list[str] = []
        yielded_content = False

        import time as _time

        from .observability import record_llm_usage

        _start = _time.monotonic()
        _in = (len(system_prompt) + sum(len(str(m.get("content", ""))) for m in messages)) // 4
        _out_chars = 0
        try:
            async with self._http.stream(
                "POST",
                f"{config.base_url}/v1/chat/completions",
                json={
                    "model": config.model,
                    "messages": oai_messages,
                    "max_tokens": config.max_tokens,
                    "temperature": config.temperature,
                    "stream": True,
                    "ttl": LM_TTL_SECONDS,
                },
                timeout=httpx.Timeout(connect=10.0, read=config.read_timeout, write=10.0, pool=10.0),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            yielded_content = True
                            _out_chars += len(content)
                            yield content
                        elif not yielded_content:
                            # Collect reasoning tokens; they become the response if no
                            # content tokens ever arrive (thinking-only model output)
                            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                            if reasoning:
                                reasoning_parts.append(reasoning)
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

            # Thinking model path: surface the reasoning text as the response
            if not yielded_content and reasoning_parts:
                logger.debug("Thinking model: surfacing %d reasoning chars as response", sum(len(p) for p in reasoning_parts))
                text = "".join(reasoning_parts)
                _out_chars += len(text)
                yield text
        finally:
            # Record token/latency metrics for the streaming chat path (was blind).
            record_llm_usage(
                model=config.model, tokens_in=_in, tokens_out=_out_chars // 4,
                latency_ms=(_time.monotonic() - _start) * 1000, route="chat",
            )
