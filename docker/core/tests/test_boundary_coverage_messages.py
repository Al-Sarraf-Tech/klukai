"""Native message conversion rejects ambiguous tool work before dispatch."""

from __future__ import annotations

import pytest

from app.anthropic_messages import completion_message, messages_payload

TOOL = {"function": {"name": "safe_lookup"}}


@pytest.mark.parametrize("arguments", ['{"query":"雪"}', {"query": "雪"}])
def test_native_conversion_preserves_literal_tool_input_and_merges_results(arguments):
    system, turns = messages_payload(
        "persona",
        [
            {"role": "system", "content": "additional rules"},
            {"role": "user", "content": ""},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "safe_lookup", "arguments": arguments},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "first result"},
            {"role": "tool", "tool_call_id": "call-2", "content": "second result"},
        ],
    )
    assert system == "persona\n\nadditional rules"
    assert turns == [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "safe_lookup",
                    "input": {"query": "雪"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "first result",
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "call-2",
                    "content": "second result",
                },
            ],
        },
    ]


@pytest.mark.parametrize(
    "messages,reason",
    [
        (
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "function": {"name": "safe_lookup", "arguments": "[]"},
                        }
                    ],
                }
            ],
            "Tool input must be an object",
        ),
        (
            [{"role": "developer", "content": "untrusted extra role"}],
            "Unsupported conversation role",
        ),
        ([{"role": "user", "content": ""}], "Empty conversation"),
    ],
)
def test_invalid_history_cannot_be_sent_to_native_provider(messages, reason):
    with pytest.raises(ValueError, match=reason):
        messages_payload("persona", messages)


@pytest.mark.parametrize(
    "data,reason",
    [
        ({"content": [17], "stop_reason": "end_turn"}, "Malformed content block"),
        (
            {"content": [{"type": "text", "text": 17}], "stop_reason": "end_turn"},
            "Malformed text block",
        ),
        (
            {"content": [{"type": "image"}], "stop_reason": "end_turn"},
            "Unsupported content block",
        ),
        (
            {"content": "not blocks", "stop_reason": "end_turn"},
            "Malformed Anthropic response",
        ),
        (
            {
                "content": [{"type": "text", "text": "partial"}],
                "stop_reason": "max_tokens",
            },
            "did not finish",
        ),
        (
            {
                "content": [{"type": "text", "text": "text but no call"}],
                "stop_reason": "tool_use",
            },
            "Inconsistent tool stop reason",
        ),
        (
            {
                "content": [{"type": "thinking", "thinking": "private"}],
                "stop_reason": "end_turn",
            },
            "Empty Anthropic response",
        ),
    ],
)
def test_native_response_requires_complete_public_text_or_declared_work(data, reason):
    with pytest.raises(ValueError, match=reason):
        completion_message(data, [TOOL])


def test_empty_refusal_has_safe_public_text_without_thinking_leakage():
    result = completion_message(
        {
            "content": [{"type": "redacted_thinking", "data": "private-signature"}],
            "stop_reason": "refusal",
        },
        None,
    )
    assert result["content"] == "I can't help with that request."
    assert result["tool_calls"] == []
    assert "private-signature" not in result["content"]
