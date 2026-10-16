"""Native Messages API conversion for Klukai's existing tool loop.

Thinking blocks are retained only in the in-memory tool conversation, never in
the text returned to the websocket or sent to a local fallback model.
"""

from __future__ import annotations
from typing import Any


import json


def messages_payload(
    system_prompt: str, messages: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    system_parts = [system_prompt]
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""
        if role == "system":
            system_parts.append(str(content))
            continue
        if role == "tool":
            blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": message["tool_call_id"],
                    "content": content,
                }
            ]
            role = "user"
        elif role == "assistant" and message.get("_anthropic_content"):
            # Preserve original thinking signatures and content ordering during
            # tool use. This field is created only by completion_message().
            blocks = message["_anthropic_content"]
        elif role in ("user", "assistant"):
            blocks = [{"type": "text", "text": content}] if content else []
            for call in message.get("tool_calls", []):
                function = call["function"]
                arguments = function.get("arguments", "{}")
                arguments = (
                    json.loads(arguments) if isinstance(arguments, str) else arguments
                )
                if not isinstance(arguments, dict):
                    raise ValueError("Tool input must be an object")
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": function["name"],
                        "input": arguments,
                    }
                )
        else:
            raise ValueError("Unsupported conversation role")
        if not blocks:
            continue
        # Parallel tool results must share the immediately following user turn.
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(blocks)
        else:
            converted.append({"role": role, "content": list(blocks)})
    if not converted:
        raise ValueError("Empty conversation")
    return "\n\n".join(system_parts), converted


def tool_definitions(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {
            "name": tool["function"]["name"],
            "description": tool["function"].get("description", ""),
            "input_schema": tool["function"].get(
                "parameters", {"type": "object", "properties": {}}
            ),
        }
        for tool in (tools or [])
    ]


def _validated_blocks(
    blocks: list[object], known_tools: set[str]
) -> tuple[list[str], list[dict[str, Any]]]:
    text: list[str] = []
    calls: list[dict[str, Any]] = []
    ids: set[str] = set()
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("Malformed content block")
        if block.get("type") == "text":
            if not isinstance(block.get("text"), str):
                raise ValueError("Malformed text block")
            text.append(block["text"])
        elif block.get("type") == "tool_use":
            name, call_id, arguments = (
                block.get("name"),
                block.get("id"),
                block.get("input"),
            )
            if (
                not isinstance(name, str)
                or name not in known_tools
                or not isinstance(call_id, str)
                or not call_id
                or call_id in ids
                or not isinstance(arguments, dict)
            ):
                raise ValueError("Invalid or undeclared tool call")
            ids.add(call_id)
            calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            )
        elif block.get("type") not in ("thinking", "redacted_thinking"):
            raise ValueError("Unsupported content block")
    return text, calls


def completion_message(
    data: dict[str, Any], tools: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Validate a complete response before exposing text or dispatching tools."""
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        raise ValueError("Malformed Anthropic response")
    stop = data.get("stop_reason")
    if stop not in ("end_turn", "tool_use", "stop_sequence", "refusal"):
        raise ValueError("Anthropic response did not finish")
    known_tools = {tool["function"]["name"] for tool in (tools or [])}
    text, calls = _validated_blocks(data["content"], known_tools)
    if bool(calls) != (stop == "tool_use"):
        raise ValueError("Inconsistent tool stop reason")
    content = "".join(text)
    if stop == "refusal" and not content.strip():
        content = "I can't help with that request."
    if not content.strip() and not calls:
        raise ValueError("Empty Anthropic response")
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": calls,
        "_anthropic_content": data["content"],
    }


def local_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep native provider metadata out of OpenAI-compatible local requests."""
    return [
        {key: value for key, value in message.items() if not key.startswith("_")}
        for message in messages
    ]
