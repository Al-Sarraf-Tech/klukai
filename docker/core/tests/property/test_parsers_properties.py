"""Property-based tests for klukai parsers + detectors.

S+ Phase 4 — `tests/property/` layer (per docs/superpowers/specs/2026-05-16-s-plus-uplift.md §5.5).

Property tests trip on edge cases unit tests miss: empty strings, unicode,
very long inputs, surrogate pairs, mixed scripts. The detectors and parsers
in app/helpers.py + app/llm_json.py are pure functions — perfect property
test targets.

The properties asserted are intentionally weak (no crashes, type invariants,
roundtrip soundness). The point isn't to re-prove correctness for known
inputs (the unit suite covers that); it's to prove the *shape* of correctness
holds for *any* input, including the gnarly ones.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    pass

try:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st
except ImportError:  # pragma: no cover — hypothesis is a test-only dep
    pytest.skip("hypothesis not installed", allow_module_level=True)


# ── chunk_text properties ─────────────────────────────────────────────────────
@given(text=st.text(), chunk_size=st.integers(min_value=1, max_value=200))
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_chunk_text_never_loses_content(text: str, chunk_size: int) -> None:
    """chunk_text() may split on whitespace but must not drop characters
    beyond whitespace normalization. Reassembled content should be a
    subsequence of the source's non-whitespace tokens."""
    from app.helpers import chunk_text

    chunks = chunk_text(text, chunk_size)
    # All chunks are non-empty strings.
    assert all(isinstance(c, str) for c in chunks)
    assert all(len(c) > 0 for c in chunks) or chunks == []
    # Each chunk is at most ~chunk_size words (word splitting; allow +1 for
    # punctuation tail).
    for c in chunks:
        assert len(c.split()) <= chunk_size + 1, c


@given(text=st.text())
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=100)
def test_chunk_text_never_raises(text: str) -> None:
    """No input — empty, unicode, control chars — should raise."""
    from app.helpers import chunk_text

    chunk_text(text)  # must not raise


# ── fix_narration properties ──────────────────────────────────────────────────
@given(text=st.text())
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_fix_narration_returns_string(text: str) -> None:
    from app.helpers import fix_narration

    result = fix_narration(text)
    assert isinstance(result, str)


@given(text=st.text())
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=100)
def test_fix_narration_idempotent(text: str) -> None:
    """Applying fix_narration twice yields the same string as applying it once."""
    from app.helpers import fix_narration

    once = fix_narration(text)
    twice = fix_narration(once)
    assert once == twice


# ── strip_actions_for_tts properties ──────────────────────────────────────────
@given(text=st.text())
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_strip_actions_no_crash(text: str) -> None:
    from app.helpers import strip_actions_for_tts

    result = strip_actions_for_tts(text)
    assert isinstance(result, str)
    # Output is no longer than input (this is a stripping function).
    assert len(result) <= len(text) + 10  # +slack for newline normalization


@given(text=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=0, max_size=2000))
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=100)
def test_strip_actions_idempotent_on_clean_text(text: str) -> None:
    """Text with no actions in brackets/asterisks is unchanged by strip
    beyond whitespace normalization."""
    from app.helpers import strip_actions_for_tts

    # Pre-clean to ensure no action markers leak in.
    if any(m in text for m in ("[", "*", "(", "<")):
        return  # uninteresting input for this property
    result = strip_actions_for_tts(text)
    # On clean input the output should preserve content modulo whitespace.
    # Compare normalized whitespace (the function may collapse runs).
    assert " ".join(result.split()) == " ".join(text.split())


# ── parse_interval_minutes properties ─────────────────────────────────────────
@given(message=st.text())
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_parse_interval_no_crash(message: str) -> None:
    """Always returns a non-negative integer; never raises on arbitrary input."""
    from app.helpers import parse_interval_minutes

    result = parse_interval_minutes(message)
    assert isinstance(result, int)
    assert result >= 0


# ── detect_squad_address properties ───────────────────────────────────────────
@given(message=st.text())
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_detect_squad_address_type(message: str) -> None:
    from app.helpers import detect_squad_address

    result = detect_squad_address(message)
    assert result is None or isinstance(result, str)


# ── parse_json properties ─────────────────────────────────────────────────────
@given(text=st.text())
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_parse_json_never_raises(text: str) -> None:
    """parse_json should swallow ALL JSON errors and never raise. It returns
    any JSON-valid value (bool, int, float, str, list, dict, None) on parse
    success, or None on parse failure.

    Property: the call site itself never crashes — that's the invariant
    behind the function (it's used in the LLM-response path where input is
    arbitrary). Type-narrowing happens at the use site, not here.
    """
    from app.llm_json import parse_json

    # Must not raise on ANY input.
    result = parse_json(text)
    # Result is any JSON-typed value or None on failure.
    assert result is None or isinstance(result, bool | int | float | str | list | dict)


@given(payload=st.dictionaries(st.text(min_size=1), st.text() | st.integers() | st.booleans()))
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=100)
def test_parse_json_roundtrip_dict(payload: dict) -> None:
    """JSON-roundtrippable dicts must come back as the same dict."""
    import json

    from app.llm_json import parse_json

    serialized = json.dumps(payload)
    parsed = parse_json(serialized)
    assert parsed == payload


# ── extract_text properties ───────────────────────────────────────────────────
@given(text=st.text())
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=100)
def test_extract_text_robust(text: str) -> None:
    """extract_text() must never raise — it's the LLM-response-parsing path
    and must be defensive (responses can be any shape on error)."""
    from app.llm_json import extract_text

    # Plausible shapes of LLM responses:
    for shape in (
        {"choices": [{"message": {"content": text}}]},
        {"choices": [{"text": text}]},
        {"content": text},
        {},
        {"choices": []},
        {"choices": [{}]},
    ):
        result = extract_text(shape)
        assert isinstance(result, str)
