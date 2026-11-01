"""Anthropic completions for chat and tool loops; routing lives on LLMRouter."""

from __future__ import annotations
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any
import asyncio
import hashlib
import json
import logging
import time
import httpx
from .models import LLMConfig
from .dependencies import http_stream, json_object
from .anthropic_messages import (
    completion_message,
    messages_payload,
    tool_definitions,
)

logger = logging.getLogger(__name__)

_RETRYABLE = (httpx.HTTPError, ValueError, TimeoutError)


def _grok_tool_calls(
    text: str, tools: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    known = {tool["function"]["name"] for tool in (tools or [])}
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict) or "tool_calls" not in data:
        return []
    raw = data.get("tool_calls")
    if not isinstance(raw, list) or not raw:
        raise ValueError("Invalid or undeclared tool call")
    calls = []
    seen = set()
    for item in raw:
        name = item.get("name") if isinstance(item, dict) else None
        arguments = item.get("arguments", {}) if isinstance(item, dict) else None
        if name not in known or not isinstance(arguments, dict) or name in seen:
            raise ValueError("Invalid or undeclared tool call")
        seen.add(name)
        calls.append(
            {
                "id": f"grok_{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        )
    return calls


class LLMCompletionsMixin(ABC):
    _anthropic_http: httpx.AsyncClient | None
    _anthropic_gate: asyncio.Semaphore

    @abstractmethod
    def _anthropic_ready(self) -> bool: ...

    @abstractmethod
    def _primary_failed(self, exc: Exception) -> None: ...

    @abstractmethod
    def _select_secondary(self, config: LLMConfig) -> bool: ...

    @abstractmethod
    def _select_grok(self, config: LLMConfig) -> None: ...

    async def stream(
        self, system_prompt: str, messages: list[dict[str, Any]], config: LLMConfig
    ) -> AsyncIterator[str]:
        """Answer with the selected model; a Claude failure falls back to Grok.

        A Claude answer is buffered and validated before anything is yielded,
        so a failure can fall back to the Grok stream without concatenating two
        answers. If no model answers, FAILURE_SENTINEL is the whole response.
        """
        from .llm_router import FAILURE_SENTINEL

        if config.provider == "grok":
            async for piece in self._stream_grok(system_prompt, messages):
                yield piece
            return
        try:
            response = await self._complete_with_fallback(
                system_prompt, messages, config, grok_fallback=False
            )
        except _RETRYABLE as exc:
            if config.provider != "anthropic" or self._anthropic_http is None:
                yield FAILURE_SENTINEL
                return
            # Nothing was yielded yet. Never log bodies, prompts or headers.
            logger.warning(
                "Claude chat failed (%s); answering with Grok", type(exc).__name__
            )
            self._select_grok(config)
            async for piece in self._stream_grok(system_prompt, messages):
                yield piece
            return
        yield response["choices"][0]["message"]["content"]

    async def _stream_grok(
        self, system_prompt: str, messages: list[dict[str, Any]]
    ) -> AsyncIterator[str]:
        """Yield Grok text. The xAI API is first; the account CLI is the fallback.

        A failure after any API token is already on the wire stops the turn.
        Appending a CLI answer would concatenate two replies.
        """
        from .grok_chat import GrokChatError, iter_text
        from .llm_router import FAILURE_SENTINEL, LLMStreamFailed
        from .xai_chat import XaiChatError
        from .xai_chat import iter_text as xai_text
        from .xai_spend import api_key, chat_backend, redact

        lines = []
        turns: list[dict[str, str]] = []
        notes: list[str] = []
        for message in messages:
            content = message.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            text_line = content if isinstance(content, str) else str(content)
            role = message.get("role")
            if role == "system" and text_line.strip():
                notes.append(text_line.strip())
            elif role in {"user", "assistant"} and text_line.strip():
                turns.append({"role": role, "content": text_line.strip()})
            lines.append(f"{role or 'user'}: {text_line}")
        prompt = "\n".join(lines).strip() or "."
        stable = getattr(system_prompt, "stable", "") or ""
        full = str(system_prompt)
        if isinstance(stable, str) and stable:
            # The persona is sent on its own even if a later edit stopped it
            # being a textual prefix of the whole prompt.
            if full.startswith(stable):
                persona, turn_state = stable, full[len(stable) :].lstrip("\n")
            else:
                persona, turn_state = stable, full
        else:
            persona, turn_state = full, ""
        history_prefix = "\n".join(
            f"{turn['role']}\n{turn['content']}" for turn in turns
        )
        logger.info(
            "chat cache sha system=%s history=%s turns=%d",
            hashlib.sha256(persona.encode()).hexdigest()[:16],
            hashlib.sha256(history_prefix.encode()).hexdigest()[:16],
            len(turns),
        )
        if notes:
            turn_state = "\n\n".join([*notes, turn_state] if turn_state else notes)
        if chat_backend() == "api" and api_key():
            yielded = False
            try:
                async for piece in xai_text(
                    persona, prompt, volatile=turn_state, turns=turns
                ):
                    yielded = True
                    yield piece
                if yielded:
                    return
            except XaiChatError as exc:
                if yielded:
                    raise LLMStreamFailed("Grok chat failed") from exc
                logger.warning(
                    "xai chat falling back to the CLI: %s",
                    redact(f"{type(exc).__name__}: {exc}"),
                )
        yielded = False
        try:
            async for piece in iter_text(system_prompt, prompt):
                yielded = True
                yield piece
        except GrokChatError as exc:
            if yielded:
                raise LLMStreamFailed("Grok chat failed") from exc
            yield FAILURE_SENTINEL
            return
        if not yielded:
            yield FAILURE_SENTINEL

    async def _complete_with_fallback(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        config: LLMConfig,
        tools: list[dict[str, Any]] | None = None,
        *,
        allow_tools: bool = True,
        grok_fallback: bool = True,
    ) -> dict[str, Any]:
        from .llm_router import GROK_MODEL, model_family

        if config.provider == "grok":
            if config.model != GROK_MODEL:
                raise ValueError("Unavailable LLM provider or model")
            # A Grok failure is terminal. Do not call a paid API.
            return await self._complete_grok(
                system_prompt, messages, config, tools, allow_tools=allow_tools
            )
        if (
            config.provider != "anthropic"
            or self._anthropic_http is None
            or model_family(config.model) is None
        ):
            # A hand-built config cannot enable another backend or model.
            logger.error(
                "Refusing unavailable LLM %r/%r", config.provider, config.model
            )
            raise ValueError("Unavailable LLM provider or model")
        try:
            return await self._complete_claude(
                system_prompt, messages, config, tools, allow_tools=allow_tools
            )
        except _RETRYABLE as exc:
            if not grok_fallback:
                raise
            logger.warning(
                "Claude chat failed (%s); answering with Grok", type(exc).__name__
            )
            self._select_grok(config)
        return await self._complete_grok(
            system_prompt, messages, config, tools, allow_tools=allow_tools
        )

    async def _complete_claude(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        config: LLMConfig,
        tools: list[dict[str, Any]] | None = None,
        *,
        allow_tools: bool = True,
    ) -> dict[str, Any]:
        """A Sonnet config falls back to Haiku; a Haiku config is tried once."""
        from .llm_router import CLAUDE_PRIMARY, model_family

        try:
            return await self._complete_anthropic(
                system_prompt, messages, config, tools, allow_tools=allow_tools
            )
        except _RETRYABLE as exc:
            if model_family(config.model) == CLAUDE_PRIMARY:
                self._primary_failed(exc)
            if not self._select_secondary(config):
                raise
        return await self._complete_anthropic(
            system_prompt, messages, config, tools, allow_tools=allow_tools
        )

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
            CHAT_MAX_TOKENS,
            CLAUDE_PRIMARY,
            ANTHROPIC_MESSAGES_URL,
            MODEL_EFFORT,
            _ANTHROPIC_MAX_RESPONSE_BYTES,
            model_family,
        )

        family = model_family(config.model)
        if self._anthropic_http is None or family is None:
            raise ValueError("Anthropic model is unavailable")
        if family == CLAUDE_PRIMARY and not self._anthropic_ready():
            raise ValueError("Anthropic primary is backing off")
        system, turns = messages_payload(system_prompt, messages)
        body = {
            "model": family,
            "system": system,
            "messages": turns,
            "max_tokens": min(max(config.max_tokens, 1), CHAT_MAX_TOKENS),
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": MODEL_EFFORT[family]},
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

    async def _complete_grok(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        config: LLMConfig,
        tools: list[dict[str, Any]] | None = None,
        *,
        allow_tools: bool = True,
    ) -> dict[str, Any]:
        from .grok_chat import MODEL, GrokChatError, chat_effort, complete_turn

        lines = []
        for message in messages:
            content = message.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            lines.append(f"{message.get('role', 'user')}: {content}")
        prompt = "\n".join(lines).strip() or "."
        system = system_prompt
        if tools and allow_tools:
            names = ", ".join(tool["function"]["name"] for tool in tools)
            system += (
                "\n\nTo call a tool, reply with only this JSON object and nothing else: "
                '{"tool_calls":[{"name":"<one of: ' + names + '>","arguments":{}}]}. '
                "Otherwise reply with the message for the user."
            )
        elif tools:
            system += "\n\nDo not call tools. Reply with the message for the user."
        try:
            turn = await complete_turn(system, prompt)
        except GrokChatError as exc:
            raise ValueError("Grok chat failed") from exc
        if turn.model != MODEL or turn.effort != chat_effort():
            raise ValueError("Unexpected Grok model or effort")
        config.model = turn.model
        calls = _grok_tool_calls(turn.text, tools) if tools and allow_tools else []
        return {
            "model": turn.model,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "" if calls else turn.text,
                        "tool_calls": calls,
                    },
                }
            ],
            "usage": {},
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
        """Tool-loop completion. Claude falls back to Grok; Grok failures raise."""
        return await self._complete_with_fallback(
            system_prompt, messages, config, tools, allow_tools=allow_tools
        )
