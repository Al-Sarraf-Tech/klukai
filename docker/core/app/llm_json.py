"""Rock-solid LLM JSON extraction. ABSOLUTE DIRECTIVE: this must never fail.

Single helper used by ALL modules that need structured output from any LLM.
Handles every known model quirk: thinking tags, reasoning fields, markdown
blocks, trailing commas, truncated strings. Returns clean dicts or defaults.
"""

from __future__ import annotations

from typing import Any


import json
import logging
import math
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


def _finite_json(value: JsonValue) -> JsonValue:
    """Reject NaN and Infinity. They are not JSON and poison stored memory."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise json.JSONDecodeError("non-finite number", "NaN", 0)
        return value
    if isinstance(value, list):
        return [_finite_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _finite_json(item) for key, item in value.items()}
    return value


def _looks_like_json_object(text: str) -> bool:
    """A prose brace (``{braces}``) is not the start of a JSON document."""
    if not text.startswith("{"):
        return False
    rest = text[1:].lstrip()
    return not rest or rest[0] in '{"}-0123456789['


def _unclosed_object(text: str) -> bool:
    """True when ``text`` is a JSON object that never finishes.

    A nested object inside that cut-off value is a fragment of the same
    document. Returning it stores the inner fact as the whole memory record.
    """
    if not _looks_like_json_object(text):
        return False
    depth = 0
    in_string = False
    escape = False
    for char in text:
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth <= 0:
                return False
    return True


def _decoded_object(text: str, pos: int = 0) -> dict[str, Any] | None:
    value, _end = _DECODER.raw_decode(text, pos)
    value = _finite_json(value)
    return value if isinstance(value, dict) else None


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
            found = _decoded_object(text, pos)
        except json.JSONDecodeError:
            # Try repairing this outer candidate before searching its children.
            # Start at the brace: unmatched quotes in leading prose are not
            # JSON string delimiters and must not alter tokenization here.
            try:
                found = _decoded_object(_repair_trailing_commas(text[pos:]))
            except json.JSONDecodeError:
                # A cut-off outer object must not donate its nested object.
                if _unclosed_object(text[pos:]):
                    return None
            else:
                if found is not None:
                    return found
        else:
            if found is not None:
                return found
        pos = text.find("{", pos + 1)
    return None


def _close_truncated(text: str) -> JsonValue | None:
    """Close a truncated object whose strings are already finished.

    Inventing the end of a cut-off string (``{"summary": "``) stores an empty
    value as if the model had finished. An empty ``{}`` invented from ``{``
    is not a document either.
    """
    if text.count('"') % 2 == 1:
        return None
    braces = text.count("{") - text.count("}")
    brackets = text.count("[") - text.count("]")
    if braces < 0 or brackets < 0 or (braces == 0 and brackets == 0):
        return None
    # Arrays close before the objects that contain them.
    fixed = text + ("]" * brackets) + ("}" * braces)
    try:
        value = _parse_value(fixed)
    except json.JSONDecodeError:
        return None
    if value == {} or value == []:
        return None
    return value


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
    return _finite_json(value)


def _fenced_value(text: str) -> JsonValue | None:
    """A fenced block that is itself JSON. A code sample must not hide a later object."""
    match = _MARKDOWN_JSON.search(text)
    if match is None:
        return None
    fenced = match.group(1).strip()
    for candidate in (fenced, _repair_trailing_commas(fenced)):
        try:
            return _parse_value(candidate)
        except json.JSONDecodeError:
            continue
    return None


def parse_json(text: str) -> JsonValue:
    """Parse any JSON value from messy LLM output, returning None on failure.

    Handles markdown blocks, embedded JSON in prose, and trailing commas.
    A cut-off string or a non-finite number is failure, not a value to store.
    """
    if not text:
        return None

    # Try direct parse first (fast path)
    text = text.strip()
    try:
        return _parse_value(text)
    except json.JSONDecodeError:
        pass

    fenced = _fenced_value(text)
    if fenced is not None:
        return fenced

    # A top-level JSON candidate may also be an array. For mixed prose, each
    # object is decoded/repaired at its own boundary by _first_object below.
    body = text
    if text.startswith(("{", "[")):
        body = _repair_trailing_commas(text)

    # Try again
    try:
        return _parse_value(body)
    except json.JSONDecodeError:
        pass
    found = _first_object(text)
    if found is not None:
        return found

    for candidate in (text, body):
        closed = _close_truncated(candidate)
        if closed is not None:
            return closed
    fenced_match = _MARKDOWN_JSON.search(text)
    if fenced_match is not None:
        closed = _close_truncated(fenced_match.group(1).strip())
        if closed is not None:
            return closed
    return None
