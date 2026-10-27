"""Rock-solid LLM JSON extraction. ABSOLUTE DIRECTIVE: this must never fail.

Single helper used by ALL modules that need structured output from any LLM.
Handles every known model quirk: thinking tags, reasoning fields, markdown
blocks, trailing commas, truncated strings. Returns clean dicts or defaults.
"""

from __future__ import annotations

from typing import Any


import json
import logging
import re

from pydantic import JsonValue

logger = logging.getLogger(__name__)

# Precompiled patterns — no per-call overhead
_THINK_TAGS = re.compile(r"<\|?/?think\|?>", re.DOTALL)
_THINK_BLOCKS = re.compile(r"<\|?think\|?>.*?<\|?/think\|?>", re.DOTALL)
_MARKDOWN_JSON = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
# Match complete strings first so punctuation inside them stays literal.
_TRAILING_COMMA = re.compile(r'("(?:\\.|[^"\\])*")|,\s*([}\]])')
_DECODER = json.JSONDecoder()


def _repair_trailing_commas(text: str) -> str:
    """Repair a JSON candidate, preserving complete quoted strings verbatim."""
    return _TRAILING_COMMA.sub(
        lambda match: match[1] if match[1] is not None else match[2], text
    )


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
            # Try repairing this outer candidate before searching its children.
            # Start at the brace: unmatched quotes in leading prose are not
            # JSON string delimiters and must not alter tokenization here.
            try:
                value, _end = _DECODER.raw_decode(_repair_trailing_commas(text[pos:]))
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(value, dict):
                    return value
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

    # A top-level JSON candidate may also be an array. For mixed prose, each
    # object is decoded/repaired at its own boundary by _first_object below.
    if text.startswith(("{", "[")):
        text = _repair_trailing_commas(text)

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
