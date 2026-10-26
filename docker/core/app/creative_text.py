"""Explicitly opt-in, capped Anthropic writing for dreams and memory scenes.

This does not inherit interactive-chat authorization or fall back to another
provider. A reserved attempt remains consumed after failures and timeouts.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from .dependencies import http_stream

import httpx

from .anthropic_messages import completion_message, messages_payload
from .creative_budget import reserve_attempt

MODEL = "claude-sonnet-5-5"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_MAX_RESPONSE_BYTES = 1024 * 1024


class CreativeTextError(RuntimeError):
    """A credential-free, terminal creative-writing failure."""


def uses_cloud_text() -> bool:
    """True for capped Sonnet writing; ``haiku`` uses the background route.

    ``local`` is the setting's old name for ``haiku``, kept so existing env
    files keep working; no local model exists any more.
    """
    provider = os.environ.get("KLUKAI_CREATIVE_TEXT_PROVIDER", "haiku").strip().lower()
    if provider not in {"haiku", "local", "anthropic"}:
        raise CreativeTextError("Unknown creative text provider")
    return provider == "anthropic"


def _completed_text(
    data: object,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if (
        not isinstance(data, dict)
        or data.get("type") != "message"
        or data.get("role") != "assistant"
        or data.get("stop_reason") != "end_turn"
        or not isinstance(data.get("model"), str)
        or not data["model"].strip()
    ):
        raise CreativeTextError("Creative text response is incomplete or refused")
    message = completion_message(data, None)
    usage = data.get("usage", {})
    if not isinstance(usage, dict):
        raise CreativeTextError("Malformed creative text usage")
    for field in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        if field in usage and (type(usage[field]) is not int or usage[field] < 0):
            raise CreativeTextError("Malformed creative text usage")
    return data, message, usage


async def complete_creative_text(system: str, user: str, max_tokens: int = 1024) -> str:
    """Return completed visible text from one authorized, budgeted request."""
    if not uses_cloud_text():
        raise CreativeTextError("Cloud creative text is disabled")
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise CreativeTextError("Creative text API key is missing")
    if (
        not isinstance(system, str)
        or not isinstance(user, str)
        or not user.strip()
        or type(max_tokens) is not int
        or not 1 <= max_tokens <= 8192
    ):
        raise CreativeTextError("Invalid creative text request")
    system_text, turns = messages_payload(system, [{"role": "user", "content": user}])
    body = {
        "model": MODEL,
        "system": system_text,
        "messages": turns,
        "max_tokens": max_tokens,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
    }
    attempt_id = await reserve_attempt("text")  # never soften cap/persistence failures
    started = time.monotonic()
    try:
        # A dedicated client prevents the local bearer token from crossing the
        # provider boundary. Canonical endpoint, no redirects or ambient proxy.
        async with asyncio.timeout(90):
            async with httpx.AsyncClient(
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                timeout=httpx.Timeout(90.0, connect=5.0),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                async with http_stream(
                    "anthropic", client, "POST", MESSAGES_URL, json=body
                ) as result:
                    result.raise_for_status()
                    raw = bytearray()
                    async for chunk in result.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > _MAX_RESPONSE_BYTES:
                            raise CreativeTextError(
                                "Creative text response exceeds size limit"
                            )
        data = json.loads(raw)
        data, message, usage = _completed_text(data)
    except CreativeTextError:
        raise
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError):
        # Never expose a response body, request prompt, or authenticated URL.
        raise CreativeTextError("Creative text request failed") from None

    from .creative_budget import record_usage

    await record_usage(attempt_id, usage)
    try:
        from .observability import record_llm_usage

        record_llm_usage(
            model=data["model"],
            tokens_in=sum(
                usage.get(field, 0)
                for field in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            ),
            tokens_out=usage.get("output_tokens", 0),
            latency_ms=(time.monotonic() - started) * 1000,
            route="creative_text",
        )
    except Exception:
        pass  # telemetry failure cannot retry an already completed paid request
    return str(message["content"]).strip()
