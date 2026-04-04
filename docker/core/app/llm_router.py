"""LLM routing: local-first with Claude API fallback."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator

import anthropic
import httpx

from .models import LLMConfig, SessionState

logger = logging.getLogger(__name__)

LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://192.168.50.2:1234")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Model aliases
LOCAL_CASUAL = "openai/gpt-oss-20b"                                    # Chat: best character fidelity
LOCAL_AGENT = "qwen3.5-27b-claude-4.6-opus-reasoning-distilled-v2"     # Agent: Opus-level tool-use + reasoning
LOCAL_TOOLS = LOCAL_AGENT                                               # Same as agent
CLOUD_COMPLEX = "claude-sonnet-4-20250514"
CLOUD_FALLBACK = "claude-haiku-4-5-20251001"

# Complexity keywords that suggest routing to Claude
COMPLEX_SIGNALS = [
    "explain", "analyze", "compare", "why", "reason", "think through",
    "help me understand", "what do you think", "philosophi", "ethic",
    "write a", "draft a", "compose",
]

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
        logger.info("LM Studio available: %s", self._lmstudio_available)
        return self._lmstudio_available

    def route(
        self,
        message: str,
        session: SessionState,
        needs_tools: bool = False,
        user_override: str | None = None,
    ) -> LLMConfig:
        """Decide which model to use for this message."""
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

    def needs_agent(self, message: str) -> bool:
        """Determine if a message needs the agentic tool-use loop."""
        if not self._lmstudio_available:
            return False  # Need LM Studio for agent loop

        lower = message.lower()
        for signal in AGENT_SIGNALS:
            if signal in lower:
                return True

        # Question marks about factual/current info
        if "?" in message and any(
            w in lower for w in ["who", "what", "where", "when", "how much", "how many"]
        ):
            # Check if it's about current/external info vs conversational
            if any(w in lower for w in [
                "you", "your", "klukai", "commander", "feel", "think about",
                "opinion", "favorite", "like", "hate",
            ]):
                return False  # Conversational question, not a tool query
            return True

        return False

    def _estimate_complexity(self, message: str, session: SessionState) -> float:
        """Heuristic complexity score 0-1."""
        score = 0.0
        lower = message.lower()

        # Length factor
        if len(message) > 500:
            score += 0.3
        elif len(message) > 200:
            score += 0.15

        # Keyword signals
        for signal in COMPLEX_SIGNALS:
            if signal in lower:
                score += 0.15
                break

        # Question depth (multiple question marks)
        q_count = message.count("?")
        if q_count > 2:
            score += 0.2
        elif q_count > 0:
            score += 0.1

        # Multi-turn depth
        if session.turn_count > 10:
            score += 0.1

        return min(score, 1.0)

    async def stream(
        self, system_prompt: str, messages: list[dict], config: LLMConfig
    ) -> AsyncIterator[str]:
        """Stream tokens from the selected LLM."""
        try:
            if config.provider == "anthropic":
                async for token in self._stream_anthropic(
                    system_prompt, messages, config
                ):
                    yield token
            else:
                async for token in self._stream_lmstudio(
                    system_prompt, messages, config
                ):
                    yield token
        except (httpx.HTTPError, anthropic.APIError) as e:
            logger.warning("LLM %s failed: %s, trying fallback", config.provider, e)
            fallback = self._get_fallback(config)
            if fallback:
                async for token in self.stream(system_prompt, messages, fallback):
                    yield token
            else:
                yield f"[Error: LLM unavailable - {e}]"

    async def complete_local(
        self,
        system_prompt: str,
        messages: list[dict],
        config: LLMConfig,
        tools: list[dict] | None = None,
    ) -> dict:
        """Non-streaming completion via LM Studio with optional tool-use.

        Returns an OpenAI-compatible response dict with:
        - choices[0].message.content (text)
        - choices[0].message.tool_calls (list of tool calls, if any)
        """
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

    def _get_fallback(self, failed: LLMConfig) -> LLMConfig | None:
        if failed.provider == "lmstudio" and self._anthropic:
            return LLMConfig(
                provider="anthropic", model=CLOUD_FALLBACK, temperature=0.7
            )
        if failed.provider == "anthropic" and self._lmstudio_available:
            return LLMConfig(
                provider="lmstudio",
                model=LOCAL_CASUAL,
                base_url=LM_STUDIO_URL,
            )
        return None

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

    async def _stream_lmstudio(
        self, system_prompt: str, messages: list[dict], config: LLMConfig
    ) -> AsyncIterator[str]:
        oai_messages = [{"role": "system", "content": system_prompt}] + messages

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
            timeout=120.0,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield content
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
