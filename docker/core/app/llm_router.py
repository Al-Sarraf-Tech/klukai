"""LLM routing: local-first with Claude API fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator

import anthropic
import httpx

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

LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://192.168.50.2:1234")       # Dominus RTX 3090
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Model aliases
LOCAL_CASUAL = "cognitivecomputations_dolphin-mistral-24b-venice-edition"  # Chat: uncensored, clean streaming, no thinking tags
LOCAL_CASUAL_FALLBACK = "dolphin-mistral-glm-4.7-flash-24b-venice-edition-thinking-uncensored-i1"  # Previous chat model
LOCAL_AGENT = "qwen3.5-27b-claude-4.6-opus-reasoning-distilled-v2"     # Agent: Opus-level tool-use + reasoning
LOCAL_TOOLS = LOCAL_AGENT                                               # Same as agent
CLOUD_COMPLEX = "claude-sonnet-4-20250514"
CLOUD_FALLBACK = "claude-haiku-4-5-20251001"

# ── Model keep-alive ───────────────────────────────────────────────────────
_MODEL_TTL = 25 * 60  # 25 minutes — keep models loaded at least this long
_KEEPALIVE_INTERVAL = 20 * 60  # Ping every 20 minutes to prevent eviction
_model_last_used: dict[str, float] = {}

# ── Idle auto-unload ──────────────────────────────────────────────────────
# If no user message for IDLE_TIMEOUT seconds AND no active mission timer,
# skip keepalive pings and let LM Studio evict models from VRAM.
IDLE_TIMEOUT = 2 * 3600  # 2 hours
_last_user_message: float = 0.0
_seeding_active: bool = False  # Set by seed_memories.py to suppress keepalive


def set_seeding_active(active: bool) -> None:
    """Signal that memory seeding is running — suppresses keepalive to avoid VRAM fights."""
    global _seeding_active
    _seeding_active = active


def _is_early_am_window() -> bool:
    """Hours 1-4 local time — proactive engine runs dreams/events, keep LLM warm."""
    from datetime import datetime
    return 1 <= datetime.now().hour <= 4


def mark_user_active() -> None:
    """Record that a user message was just received (for idle unload)."""
    global _last_user_message
    _last_user_message = time.monotonic()


def _is_user_idle() -> bool:
    """True if no user message received for IDLE_TIMEOUT seconds."""
    if _last_user_message == 0.0:
        return False  # Never messaged yet — don't unload on fresh startup
    return (time.monotonic() - _last_user_message) > IDLE_TIMEOUT


def mark_model_used(model: str) -> None:
    """Record that a model was just used (for keepalive scheduling)."""
    _model_last_used[model] = time.monotonic()


def model_needs_keepalive(model: str) -> bool:
    """True if the model hasn't been used recently and may be evicted."""
    last = _model_last_used.get(model, 0)
    return last == 0 or (time.monotonic() - last > _KEEPALIVE_INTERVAL)

# Signals that the message needs tool use (agent loop)
AGENT_SIGNALS = [
    "search", "look up", "find out", "what's happening", "current",
    "latest", "news", "weather", "browse", "check online",
    "what time", "today's", "right now", "this week",
    "who is", "what is the price", "stock", "score",
]


class LLMRouter:
    """Routes requests to local LM Studio or Anthropic based on complexity."""

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._anthropic: anthropic.AsyncAnthropic | None = None
        self._lmstudio_available: bool | None = None
        self._lmstudio_last_check: float = 0.0

    async def init(self) -> None:
        self._http = httpx.AsyncClient(timeout=60.0)
        if ANTHROPIC_API_KEY:
            self._anthropic = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
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

        # User explicit override
        if user_override:
            if user_override.startswith("claude"):
                return LLMConfig(
                    provider="anthropic", model=user_override, temperature=0.7
                )
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

        # Fallback: Claude if no local
        if self._anthropic:
            return LLMConfig(
                provider="anthropic", model=CLOUD_FALLBACK, temperature=0.7
            )

        raise RuntimeError("No LLM backend available")

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
        """Stream tokens from the selected LLM.

        LM Studio calls go through a global gate (1-at-a-time) to prevent
        queue pile-up when the server is slow or swapping models.
        On local failure, falls back directly to Claude — no cascading
        local retries that would just pile more requests onto a stuck server.
        """
        if config.provider == "anthropic":
            async for token in self._stream_anthropic(
                system_prompt, messages, config
            ):
                yield token
            mark_model_used(config.model)
            return

        # ── LM Studio path: acquire gate so only 1 request is in-flight ──
        gate = get_lm_gate()
        async with gate:
            try:
                async for token in self._stream_lmstudio(
                    system_prompt, messages, config
                ):
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
            except Exception as e:
                error_msg = str(e) or type(e).__name__
                logger.warning(
                    "LLM %s/%s failed: %s — skipping local retries",
                    config.provider, config.model, error_msg,
                )
                self._lmstudio_available = False
                self._lmstudio_last_check = time.monotonic()
        # ── Gate released ──

        # Cloud fallback (outside gate — Anthropic is a different server)
        if self._anthropic:
            logger.info("Fast-fallback to Claude after local failure")
            cloud = LLMConfig(provider="anthropic", model=CLOUD_FALLBACK, temperature=0.7)
            async for token in self._stream_anthropic(system_prompt, messages, cloud):
                yield token
        else:
            yield "Communications disrupted, Commander. Standby for reconnection."

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
            }
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"

            r = await self._http.post(
                f"{config.base_url}/v1/chat/completions",
                json=body,
                timeout=120.0,
            )
            r.raise_for_status()
            return r.json()

    async def _stream_anthropic(
        self, system_prompt: str, messages: list[dict], config: LLMConfig
    ) -> AsyncIterator[str]:
        async with self._anthropic.messages.stream(
            model=config.model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            system=system_prompt,
            messages=messages,
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def keepalive(self) -> None:
        """Ping primary chat model to keep it loaded in LM Studio VRAM.

        Skips if a real request is already in-flight (model is warm by definition).
        Also skips if the user has been idle for IDLE_TIMEOUT and no mission is active,
        letting LM Studio evict models to free VRAM.
        """
        if lm_gate_busy():
            return  # Real request in progress — model is warm, skip ping
        if not self._lmstudio_available:
            await self._ensure_lmstudio_fresh()
        if not self._lmstudio_available:
            return

        # Skip keepalive during memory seeding — it fights for VRAM
        if _seeding_active:
            logger.debug("Keepalive skipped: memory seeding active")
            return

        # Auto-unload: skip keepalive when no users connected AND no mission running
        # Exception: early AM (1-4) — proactive engine needs LLM for dreams/events
        from .context import ws
        anyone_connected = bool(ws._connections)
        from .proactive import has_active_mission

        if not anyone_connected and not has_active_mission() and not _is_early_am_window():
            if _is_user_idle():
                logger.info("LLM idle unload: no connections, no mission, letting models evict")
                return

        gate = get_lm_gate()
        # Only keepalive dolphin — it handles chat + extraction + classification
        for model in (LOCAL_CASUAL,):
            if not model_needs_keepalive(model):
                continue
            try:
                async with gate:
                    r = await self._http.post(
                        f"{LM_STUDIO_URL}/v1/chat/completions",
                        json={
                            "model": model,
                            "messages": [{"role": "user", "content": "."}],
                            "max_tokens": 1,
                            "temperature": 0,
                            "stream": False,
                        },
                        timeout=30.0,
                    )
                    if r.status_code == 200:
                        mark_model_used(model)
                        logger.debug("Keepalive OK: %s", model)
            except Exception as e:
                logger.warning("Keepalive failed for %s: %s", model, e)

    async def _stream_lmstudio(
        self, system_prompt: str, messages: list[dict], config: LLMConfig
    ) -> AsyncIterator[str]:
        oai_messages = [{"role": "system", "content": system_prompt}] + messages

        # Some models (e.g. dolphin-mistral-glm thinking variants) emit all tokens
        # under reasoning_content and leave content empty.  We collect reasoning
        # tokens as a fallback and yield them if no content tokens appear at all.
        reasoning_parts: list[str] = []
        yielded_content = False

        async with self._http.stream(
            "POST",
            f"{config.base_url}/v1/chat/completions",
            json={
                "model": config.model,
                "messages": oai_messages,
                "max_tokens": config.max_tokens,
                "temperature": config.temperature,
                "stream": True,
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
            yield "".join(reasoning_parts)
