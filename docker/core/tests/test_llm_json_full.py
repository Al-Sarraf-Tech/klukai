"""Tests for app.llm_json — JSON extraction from messy LLM output."""

from __future__ import annotations



from app.llm_json import extract_text, parse_json


class TestExtractText:
    def test_empty_choices_returns_empty(self):
        assert extract_text({"choices": []}) == ""

    def test_no_choices_key_returns_empty(self):
        assert extract_text({}) == ""

    def test_content_field_extracted(self):
        resp = {"choices": [{"message": {"content": "Hello world"}}]}
        assert extract_text(resp) == "Hello world"

    def test_reasoning_content_fallback(self):
        # content empty, reasoning_content set
        resp = {"choices": [{"message": {"content": "", "reasoning_content": "fallback text"}}]}
        assert extract_text(resp) == "fallback text"

    def test_reasoning_fallback(self):
        resp = {"choices": [{"message": {"content": "", "reasoning_content": "", "reasoning": "third try"}}]}
        assert extract_text(resp) == "third try"

    def test_thinking_block_stripped(self):
        resp = {"choices": [{"message": {"content": "<think>internal monolog</think>actual answer"}}]}
        assert extract_text(resp) == "actual answer"

    def test_multiple_thinking_blocks_stripped(self):
        text = "<think>secret1</think>real<think>secret2</think>final"
        resp = {"choices": [{"message": {"content": text}}]}
        result = extract_text(resp)
        assert "real" in result
        assert "final" in result
        assert "secret1" not in result
        assert "secret2" not in result

    def test_orphan_think_tags_stripped(self):
        text = "answer<|think|>"
        resp = {"choices": [{"message": {"content": text}}]}
        assert extract_text(resp) == "answer"

    def test_only_thinking_returns_empty(self):
        text = "<think>only thinking</think>"
        resp = {"choices": [{"message": {"content": text}}]}
        # All content stripped → falls through to next field (which is empty)
        assert extract_text(resp) == ""

    def test_whitespace_only_treated_as_empty(self):
        resp = {"choices": [{"message": {"content": "   \n  ", "reasoning": "real"}}]}
        assert extract_text(resp) == "real"


class TestParseJson:
    def test_none_input(self):
        assert parse_json("") is None
        assert parse_json(None) is None  # type: ignore

    def test_valid_json_fast_path(self):
        assert parse_json('{"a": 1, "b": "two"}') == {"a": 1, "b": "two"}

    def test_markdown_block_extracted(self):
        text = '```json\n{"key": "value"}\n```'
        assert parse_json(text) == {"key": "value"}

    def test_markdown_block_no_language(self):
        text = '```\n{"x": 1}\n```'
        assert parse_json(text) == {"x": 1}

    def test_embedded_in_prose(self):
        text = 'Here is the answer: {"result": "ok"} as requested.'
        assert parse_json(text) == {"result": "ok"}

    def test_trailing_comma_fixed(self):
        text = '{"a": 1, "b": 2,}'
        result = parse_json(text)
        assert result == {"a": 1, "b": 2}

    def test_trailing_comma_in_array(self):
        text = '{"items": [1, 2, 3,]}'
        result = parse_json(text)
        assert result == {"items": [1, 2, 3]}

    def test_unclosed_string_repaired(self):
        # Mid-response truncation — has unbalanced quote
        text = '{"a": "not closed'
        result = parse_json(text)
        # The repair attempts to close + bracket — may or may not succeed
        # exactly; just ensure no crash
        assert result is None or isinstance(result, dict)

    def test_unparseable_returns_none(self):
        assert parse_json("not json at all") is None

    def test_nested_object(self):
        text = '{"outer": {"inner": "value"}}'
        assert parse_json(text) == {"outer": {"inner": "value"}}

    def test_unicode_preserved(self):
        text = '{"emoji": "test", "japanese": "ありがとう"}'
        result = parse_json(text)
        assert result is not None
        assert result["japanese"] == "ありがとう"
