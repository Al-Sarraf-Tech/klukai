"""Provider completions and streaming; routing and policy remain on LLMRouter."""

from __future__ import annotations
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any
import asyncio
import json
import logging
import time
import httpx
from .models import LLMConfig
from .lm_gateway import LM_CONNECT_TIMEOUT_SECONDS, LM_TTL_SECONDS, lm_timeout
from .dependencies import http_request, http_stream, json_object
from .anthropic_messages import (
    completion_message,
    local_messages,
    messages_payload,
    tool_definitions,
)

logger = logging.getLogger(__name__)


class LLMCompletionsMixin(ABC):
    _http: httpx.AsyncClient | None
    _anthropic_http: httpx.AsyncClient | None
    _anthropic_gate: asyncio.Semaphore
    _lmstudio_available: bool | None
    _lmstudio_last_check: float
    _game_active_last_check: float

    @abstractmethod
    def _anthropic_ready(self) -> bool: ...

    @abstractmethod
    def _primary_failed(self, exc: Exception) -> None: ...

    @abstractmethod
    async def _select_local_secondary(
        self, config: LLMConfig, needs_tools: bool = False
    ) -> None: ...

    async def stream(
        self, system_prompt: str, messages: list[dict[str, Any]], config: LLMConfig
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
        from .llm_router import (
            get_lm_gate,
            mark_model_used,
            FAILURE_SENTINEL,
            LLMStreamFailed,
        )

        yielded = False

        if config.provider == "anthropic" and self._anthropic_http is not None:
            try:
                response = await self._complete_anthropic(
                    system_prompt, messages, config
                )
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
                    config.provider,
                    config.model,
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
                    config.provider,
                    config.model,
                    e.response.status_code,
                )
                self._game_active_last_check = 0.0
                local_error = e
            except Exception as e:
                error_msg = str(e) or type(e).__name__
                logger.warning(
                    "LLM %s/%s failed: %s — skipping local retries",
                    config.provider,
                    config.model,
                    error_msg,
                )
                self._lmstudio_available = False
                self._lmstudio_last_check = time.monotonic()
                local_error = e
        # ── Gate released ──

        if yielded:
            # Mid-stream death after partial local tokens: surface the failure
            # rather than inventing a second answer.
            raise LLMStreamFailed("local stream died mid-response") from local_error

        # The local secondary is the final backend.
        yield FAILURE_SENTINEL

    async def _complete_anthropic(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        config: LLMConfig,
        tools: list[dict[str, Any]] | None = None,
        *,
        allow_tools: bool = True,
    ) -> dict[str, Any]:
        from .llm_router import (
            CLAUDE_PRIMARY,
            ANTHROPIC_MESSAGES_URL,
            _ANTHROPIC_MAX_RESPONSE_BYTES,
        )

        if not self._anthropic_ready():
            raise ValueError("Anthropic primary is unavailable")
        system, turns = messages_payload(system_prompt, messages)
        body = {
            "model": CLAUDE_PRIMARY,
            "system": system,
            "messages": turns,
            "max_tokens": min(max(config.max_tokens, 1), 8192),
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "medium"},
        }
        if tools:
            body["tools"] = tool_definitions(tools)
            if not allow_tools:
                body["tool_choice"] = {"type": "none"}
        start = time.monotonic()
        # Bound both queueing and HTTP lifetime. Cancellation propagates rather
        # than starting a second inference after the websocket disconnects.
        async with asyncio.timeout(min(config.read_timeout, 90.0)):
            async with self._anthropic_gate:
                async with http_stream(
                    "anthropic",
                    self._anthropic_http,
                    "POST",
                    ANTHROPIC_MESSAGES_URL,
                    json=body,
                ) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > _ANTHROPIC_MAX_RESPONSE_BYTES:
                            raise ValueError("Anthropic response exceeds size limit")
        data = json_object(json.loads(raw))
        message = completion_message(data, tools if allow_tools else None)
        if not isinstance(data.get("model"), str) or not data["model"]:
            raise ValueError("Missing response model")
        config.model = data["model"]
        usage = data.get("usage") or {}
        try:
            from .observability import record_llm_usage

            record_llm_usage(
                model=config.model,
                tokens_in=sum(
                    int(usage.get(key, 0) or 0)
                    for key in (
                        "input_tokens",
                        "cache_creation_input_tokens",
                        "cache_read_input_tokens",
                    )
                ),
                tokens_out=int(usage.get("output_tokens", 0) or 0),
                latency_ms=(time.monotonic() - start) * 1000,
                route="chat",
            )
        except Exception:
            pass
        return {
            "model": config.model,
            "choices": [{"message": message}],
            "usage": usage,
        }

    async def complete_chat(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        config: LLMConfig,
        tools: list[dict[str, Any]] | None = None,
        *,
        allow_tools: bool = True,
    ) -> dict[str, Any]:
        """Interactive tool-loop completion with a local secondary."""
        if config.provider == "anthropic" and self._anthropic_http is not None:
            try:
                return await self._complete_anthropic(
                    system_prompt, messages, config, tools, allow_tools=allow_tools
                )
            except (httpx.HTTPError, ValueError, TimeoutError) as exc:
                self._primary_failed(exc)
                await self._select_local_secondary(config, needs_tools=bool(tools))
        if config.provider != "lmstudio":
            raise ValueError("Unavailable LLM provider")
        return await self.complete_local(
            system_prompt, messages, config, tools if allow_tools else None
        )

    async def complete_local(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        config: LLMConfig,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Non-streaming completion via LM Studio with optional tool-use.

        Gated: only 1 LM Studio request at a time.

        Returns an OpenAI-compatible response dict with:
        - choices[0].message.content (text)
        - choices[0].message.tool_calls (list of tool calls, if any)
        """
        from .llm_router import get_lm_gate

        if config.provider != "lmstudio":
            raise ValueError("complete_local requires a local provider")
        gate = get_lm_gate()
        async with gate:
            oai_messages = [
                {"role": "system", "content": system_prompt}
            ] + local_messages(messages)

            body: dict[str, Any] = {
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
            r = await http_request(
                "lm_studio",
                self._http,
                "post",
                f"{config.base_url}/v1/chat/completions",
                json=body,
                timeout=lm_timeout(120.0),
            )
            r.raise_for_status()
            data = json_object(r.json())
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
            return json_object(data)

    async def keepalive(self) -> None:
        """Compatibility no-op: strict policy forbids extending LLM residency."""
        logger.warning(
            "LLM keepalive ignored: strict max idle TTL is %ss", LM_TTL_SECONDS
        )

    async def _stream_lmstudio(
        self, system_prompt: str, messages: list[dict[str, Any]], config: LLMConfig
    ) -> AsyncIterator[str]:
        oai_messages = [{"role": "system", "content": system_prompt}] + local_messages(
            messages
        )

        # Some models (e.g. dolphin-mistral-glm thinking variants) emit all tokens
        # under reasoning_content and leave content empty.  We collect reasoning
        # tokens as a fallback and yield them if no content tokens appear at all.
        reasoning_parts: list[str] = []
        yielded_content = False

        import time as _time

        from .observability import record_llm_usage

        _start = _time.monotonic()
        _in = (
            len(system_prompt) + sum(len(str(m.get("content", ""))) for m in messages)
        ) // 4
        _out_chars = 0
        try:
            async with http_stream(
                "lm_studio",
                self._http,
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
                timeout=httpx.Timeout(
                    connect=LM_CONNECT_TIMEOUT_SECONDS,
                    read=config.read_timeout,
                    write=10.0,
                    pool=10.0,
                ),
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
                            reasoning = delta.get("reasoning_content") or delta.get(
                                "reasoning"
                            )
                            if reasoning:
                                reasoning_parts.append(reasoning)
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

            # Thinking model path: surface the reasoning text as the response
            if not yielded_content and reasoning_parts:
                logger.debug(
                    "Thinking model: surfacing %d reasoning chars as response",
                    sum(len(p) for p in reasoning_parts),
                )
                text = "".join(reasoning_parts)
                _out_chars += len(text)
                yield text
        finally:
            # Record token/latency metrics for the streaming chat path (was blind).
            record_llm_usage(
                model=config.model,
                tokens_in=_in,
                tokens_out=_out_chars // 4,
                latency_ms=(_time.monotonic() - _start) * 1000,
                route="chat",
            )
