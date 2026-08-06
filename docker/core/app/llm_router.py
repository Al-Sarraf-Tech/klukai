"""LLM routing: local RTX 3090 only. No cloud fallback — ever."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator

import httpx

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
# Cloud fallback is intentionally absent. Owner policy: all inference stays
# on the local RTX 3090. ANTHROPIC_API_KEY is ignored if present.

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
    Failures before the first token degrade in-band to FAILURE_SENTINEL only
    (never a cloud model).
    """


# Signals that the message needs tool use (agent loop)
AGENT_SIGNALS = [
    "search", "look up", "find out", "what's happening", "current",
    "latest", "news", "weather", "browse", "check online",
    "what time", "today's", "right now", "this week",
    "who is", "what is the price", "stock", "score",
]


class LLMRouter:
    """Routes requests to local LM Studio only. Cloud is not an option."""

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._lmstudio_available: bool | None = None
        self._lmstudio_last_check: float = 0.0

    async def init(self) -> None:
        headers = _lm_headers()
        self._http = httpx.AsyncClient(timeout=60.0, headers=headers if headers else None)
        # Deliberately never constructs an Anthropic client, even if
        # ANTHROPIC_API_KEY is set in the environment.
        await self._check_lmstudio()

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()

    async def _check_lmstudio(self) -> bool:
        try:
            r = await self._http.get(f"{LM_STUDIO_URL}/v1/models", timeout=5.0)
            self._lmstudio_available = r.status_code == 200
        except httpx.HTTPError:
            self._lmstudio_available = False
        self._lmstudio_last_check = time.monotonic()
        logger.info("LM Studio available: %s", self._lmstudio_available)
        return self._lmstudio_available

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
    ) -> LLMConfig:
        """Decide which model to use for this message."""
        # Re-check LM Studio if it was previously down
        if not self._lmstudio_available:
            await self._ensure_lmstudio_fresh()

        # User explicit override — local models only. Cloud model names are
        # rejected so a stale client/UI preference can never leave the box.
        if user_override:
            if user_override.startswith("claude") or user_override.startswith("anthropic"):
                logger.warning(
                    "Ignoring cloud model override %r — local-only policy",
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
                model=LOCAL_TOOLS,
                base_url=LM_STUDIO_URL,
            )

        # Default: gpt-oss-20b for all chat (casual + complex)
        if self._lmstudio_available:
            return LLMConfig(
                provider="lmstudio",
                model=LOCAL_CASUAL,
                base_url=LM_STUDIO_URL,
            )

        # No cloud fallback. Ever. If the local GPU path is down, fail closed.
        raise RuntimeError("No local LLM backend available")

    async def needs_agent(self, message: str) -> bool:
        """Determine if a message needs the agentic tool-use loop."""
        if not self._lmstudio_available:
            await self._ensure_lmstudio_fresh()
        if not self._lmstudio_available:
            return False  # Need LM Studio for agent loop

        lower = message.lower()

        # RP/conversational context words — if any are present, the message
        # is almost certainly in-character and should NOT route to agent.
        rp_context = any(w in lower for w in [
            "you", "your", "klukai", "commander", "feel", "think",
            "opinion", "favorite", "like", "hate", "they", "them",
            "squad", "mission", "team", "soldier", "t-doll",
            "remember", "discover", "did we", "did she", "did he",
            "would", "could", "should", "shall",
            "base", "hq", "camp", "sector", "area", "weapon",
        ])

        for signal in AGENT_SIGNALS:
            if signal in lower:
                # Even if signal matches, RP context overrides (e.g., "who is the squad leader?")
                if rp_context:
                    return False
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
        """Stream tokens from the local LLM only.

        LM Studio calls go through a global gate (1-at-a-time) to prevent
        queue pile-up when the server is slow or swapping models.
        On local failure there is no cloud path — only FAILURE_SENTINEL.

        Failure semantics: a failure BEFORE any token yields FAILURE_SENTINEL
        as the whole response. A failure AFTER tokens were yielded raises
        LLMStreamFailed so a partial answer is never silently concatenated
        with a second full response.
        """
        yielded = False

        if config.provider != "lmstudio":
            # Defense in depth: even a hand-built LLMConfig cannot leave the box.
            logger.error(
                "Refusing non-local LLM provider %r (local-only policy)",
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

        # Local path is the only path. No Anthropic, no second model, no off-box.
        yield FAILURE_SENTINEL

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
        gate = get_lm_gate()
        async with gate:
            oai_messages = [{"role": "system", "content": system_prompt}] + messages

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
        oai_messages = [{"role": "system", "content": system_prompt}] + messages

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
