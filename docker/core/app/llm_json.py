"""Rock-solid LLM JSON extraction. ABSOLUTE DIRECTIVE: this must never fail.

Single helper used by ALL modules that need structured output from any LLM.
Handles every known model quirk: thinking tags, reasoning fields, markdown
blocks, trailing commas, truncated strings. Returns clean dicts or defaults.
"""

from __future__ import annotations

from .dependencies import http_request
from typing import Any


import json
import logging
import re

from pydantic import JsonValue

import httpx

from .lm_gateway import LM_TTL_SECONDS, lm_studio_auth_headers, lm_timeout

logger = logging.getLogger(__name__)

# Precompiled patterns — no per-call overhead
_THINK_TAGS = re.compile(r"<\|?/?think\|?>", re.DOTALL)
_THINK_BLOCKS = re.compile(r"<\|?think\|?>.*?<\|?/think\|?>", re.DOTALL)
_MARKDOWN_JSON = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_DECODER = json.JSONDecoder()


def _first_object(text: str) -> dict[str, Any] | None:
    """Return the first complete JSON object embedded anywhere in ``text``.

    Replaces a one-level regex that could not see past depth-2 nesting (it
    returned the inner object) and was skipped entirely when the text began
    with ``{`` — so an object followed by a sign-off line parsed as nothing.
    raw_decode handles arbitrary depth and ignores whatever follows.
    """
    pos = text.find("{")
    while pos != -1:
        try:
            value, _end = _DECODER.raw_decode(text, pos)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(value, dict):
                return value
        pos = text.find("{", pos + 1)
    return None


def extract_text(response: dict[str, Any]) -> str:
    """Extract usable text from any LLM response, regardless of model quirks.

    Checks: content → reasoning_content → reasoning → all combined.
    Strips thinking tags and returns clean text.
    """
    choices = response.get("choices", [])
    if not choices:
        return ""

    msg = choices[0].get("message", {})

    # Try each field in priority order
    for field in ("content", "reasoning_content", "reasoning"):
        text = (msg.get(field) or "").strip()
        if text:
            # Strip thinking blocks, keep the rest
            text = _THINK_BLOCKS.sub("", text).strip()
            # Strip any remaining orphan think tags
            text = _THINK_TAGS.sub("", text).strip()
            if text:
                return text

    return ""


def _parse_value(text: str) -> JsonValue:
    """The standard JSON decoder guarantees the recursive JSON value domain."""
    value: JsonValue = json.loads(text)
    return value


def parse_json(text: str) -> JsonValue:
    """Parse any JSON value from messy LLM output, returning None on failure.

    Handles: markdown blocks, embedded JSON in prose, trailing commas,
    single quotes, truncated strings.
    """
    if not text:
        return None

    # Try direct parse first (fast path)
    text = text.strip()
    try:
        return _parse_value(text)
    except json.JSONDecodeError:
        pass

    # Strip markdown code blocks
    md_match = _MARKDOWN_JSON.search(text)
    if md_match:
        try:
            return _parse_value(md_match.group(1).strip())
        except json.JSONDecodeError:
            text = md_match.group(1).strip()

    # Find a complete JSON object in mixed text (prose before or after it,
    # any nesting depth).
    found = _first_object(text)
    if found is not None:
        return found

    # Repair common issues, then look again
    text = _TRAILING_COMMA.sub(r"\1", text)

    # Try again
    try:
        return _parse_value(text)
    except json.JSONDecodeError:
        pass
    found = _first_object(text)
    if found is not None:
        return found

    # Last resort: fix truncated strings by closing them
    try:
        # Find last unclosed quote and close it
        fixed = text
        if fixed.count('"') % 2 == 1:
            fixed += '"'
        # Close any unclosed brackets
        opens = fixed.count("{") - fixed.count("}")
        fixed += "}" * max(0, opens)
        opens = fixed.count("[") - fixed.count("]")
        fixed += "]" * max(0, opens)
        return _parse_value(fixed)
    except json.JSONDecodeError:
        return None


async def call_llm(  # pragma: no cover - integration (LM Studio HTTP)
    url: str,
    model: str,
    prompt: str,
    *,
    max_tokens: int = 2048,
    temperature: float = 0.1,
    system: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Call LLM and return parsed JSON. Never raises — returns empty dict on failure.

    This is the ONE function all modules use for structured LLM output.
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    http = client or httpx.AsyncClient(timeout=lm_timeout(120.0))
    close_after = client is None

    try:
        r = await http_request(
            "lm_studio",
            http,
            "post",
            f"{url}/v1/chat/completions",
            headers=lm_studio_auth_headers(),
            json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
                "ttl": LM_TTL_SECONDS,
            },
        )
        r.raise_for_status()

        text = extract_text(r.json())
        if not text:
            logger.warning("LLM returned empty response (model=%s)", model)
            return {}

        result = parse_json(text)
        if not isinstance(result, dict):
            logger.warning(
                "Failed to parse JSON from LLM (model=%s): %s", model, text[:100]
            )
            return {}

        return result

    except httpx.HTTPStatusError as e:
        logger.warning("LLM HTTP error (model=%s): %s", model, e.response.status_code)
        return {}
    except Exception as e:
        logger.warning("LLM call failed (model=%s, %s)", model, type(e).__name__)
        return {}
    finally:
        if close_after:
            await http.aclose()


async def call_llm_text(  # pragma: no cover - integration (LM Studio HTTP)
    url: str,
    model: str,
    prompt: str,
    *,
    max_tokens: int = 200,
    temperature: float = 0.5,
    system: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Call LLM and return plain text. Never raises — returns empty string on failure.

    For non-JSON responses (summaries, mission updates, romance messages).
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    http = client or httpx.AsyncClient(timeout=lm_timeout(120.0))
    close_after = client is None

    try:
        r = await http_request(
            "lm_studio",
            http,
            "post",
            f"{url}/v1/chat/completions",
            headers=lm_studio_auth_headers(),
            json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
                "ttl": LM_TTL_SECONDS,
            },
        )
        r.raise_for_status()

        text = extract_text(r.json())
        return text or ""

    except Exception as e:
        logger.warning("LLM text call failed (model=%s, %s)", model, type(e).__name__)
        return ""
    finally:
        if close_after:
            await http.aclose()
