"""Malformed tool fields cannot discard neighboring readable results."""

import pytest

from app.tool_result_text import _extract_tool_text


@pytest.mark.parametrize("value", [None, 42, {}, [], False])
@pytest.mark.parametrize("typed", [True, False])
def test_malformed_text_fields_preserve_valid_siblings(value, typed):
    malformed = {"text": value}
    if typed:
        malformed["type"] = "text"
    result = {"content": ["First", malformed, {"text": "Last"}]}
    assert _extract_tool_text(result) == "First\nLast"


@pytest.mark.parametrize("value", [None, 42, {}, [], False])
def test_only_malformed_text_uses_error_fallback(value):
    result = {"content": [{"type": "text", "text": value}], "error": "Unavailable"}
    assert _extract_tool_text(result) == "Error: Unavailable"


@pytest.mark.parametrize("value", [None, 42, {}, [], False])
def test_only_malformed_text_uses_existing_repr_fallback(value):
    result = {"content": [{"text": value}]}
    assert _extract_tool_text(result) == str(result)


@pytest.mark.parametrize(
    "item", [{"type": "text"}, {"type": "text", "text": ""}, {"text": ""}]
)
def test_empty_text_preserves_existing_precedence_over_error(item):
    assert _extract_tool_text({"content": [item], "error": "Unavailable"}) == ""


def test_normal_text_preserves_markup_cleanup_and_error_precedence():
    result = {
        "content": [{"type": "text", "text": "<b>Readable</b>"}],
        "error": "Ignored",
    }
    assert _extract_tool_text(result) == "Readable"
