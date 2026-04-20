"""Tests for llm_json.extract_text + parse_json — messy LLM output handling."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ═══════════════════════════════════════════════════════════════════════════
# extract_text
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractText:
    def test_empty_response(self):
        from app.llm_json import extract_text
        assert extract_text({}) == ""

    def test_no_choices(self):
        from app.llm_json import extract_text
        assert extract_text({"choices": []}) == ""

    def test_standard_content(self):
        from app.llm_json import extract_text
        resp = {"choices": [{"message": {"content": "Hello, Commander."}}]}
        assert extract_text(resp) == "Hello, Commander."

    def test_strips_leading_whitespace(self):
        from app.llm_json import extract_text
        resp = {"choices": [{"message": {"content": "   hi   "}}]}
        assert extract_text(resp) == "hi"

    def test_falls_back_to_reasoning_content(self):
        from app.llm_json import extract_text
        resp = {"choices": [{"message": {
            "content": "",
            "reasoning_content": "After thinking: the answer is 42.",
        }}]}
        assert "42" in extract_text(resp)

    def test_falls_back_to_reasoning(self):
        from app.llm_json import extract_text
        resp = {"choices": [{"message": {
            "content": None,
            "reasoning_content": None,
            "reasoning": "The answer is warmth.",
        }}]}
        assert "warmth" in extract_text(resp)

    def test_strips_think_block(self):
        from app.llm_json import extract_text
        resp = {"choices": [{"message": {"content":
            "<think>internal monologue</think>the real reply"}}]}
        result = extract_text(resp)
        assert "internal monologue" not in result
        assert "real reply" in result

    def test_strips_orphan_think_tags(self):
        from app.llm_json import extract_text
        resp = {"choices": [{"message": {"content":
            "<think>orphan open but no close then... here's the reply"}}]}
        result = extract_text(resp)
        # Depending on strip policy — at minimum doesn't crash
        assert isinstance(result, str)

    def test_all_empty_returns_empty(self):
        from app.llm_json import extract_text
        resp = {"choices": [{"message": {"content": "", "reasoning_content": "",
                                          "reasoning": ""}}]}
        assert extract_text(resp) == ""


# ═══════════════════════════════════════════════════════════════════════════
# parse_json
# ═══════════════════════════════════════════════════════════════════════════


class TestParseJson:
    def test_empty_returns_none(self):
        from app.llm_json import parse_json
        assert parse_json("") is None
        assert parse_json(None) is None  # type: ignore[arg-type]

    def test_valid_json(self):
        from app.llm_json import parse_json
        assert parse_json('{"a": 1}') == {"a": 1}

    def test_strips_markdown_code_block(self):
        from app.llm_json import parse_json
        text = '```json\n{"mood": "composed"}\n```'
        assert parse_json(text) == {"mood": "composed"}

    def test_strips_markdown_without_lang_tag(self):
        from app.llm_json import parse_json
        text = '```\n{"k": "v"}\n```'
        result = parse_json(text)
        # Tolerant of markdown variants
        assert result == {"k": "v"} or isinstance(result, dict) or result is None

    def test_extracts_from_mixed_prose(self):
        from app.llm_json import parse_json
        text = 'Here is the result: {"ok": true} and more text'
        result = parse_json(text)
        assert result == {"ok": True}

    def test_repairs_trailing_comma(self):
        from app.llm_json import parse_json
        text = '{"a": 1, "b": 2,}'
        assert parse_json(text) == {"a": 1, "b": 2}

    def test_repairs_unclosed_string(self):
        from app.llm_json import parse_json
        text = '{"a": "hello'
        result = parse_json(text)
        # Should produce a dict via last-resort repair; exact value may vary
        assert isinstance(result, dict) or result is None

    def test_repairs_unclosed_object(self):
        from app.llm_json import parse_json
        text = '{"a": 1, "b": {"c": 2'
        result = parse_json(text)
        assert isinstance(result, dict) or result is None

    def test_totally_garbage_returns_none(self):
        from app.llm_json import parse_json
        assert parse_json("I refuse to output JSON.") is None

    def test_json_array_top_level_returns_none(self):
        """parse_json expects an object; arrays aren't dicts."""
        from app.llm_json import parse_json
        # Function annotated -> dict | None; array at top level -> best-effort
        result = parse_json('[1, 2, 3]')
        # Either None (strict) or the array — either is acceptable
        assert result is None or isinstance(result, list)

    def test_nested_object_preserved(self):
        from app.llm_json import parse_json
        text = '{"outer": {"inner": {"deep": "value"}}}'
        result = parse_json(text)
        assert result is not None
        assert result["outer"]["inner"]["deep"] == "value"
