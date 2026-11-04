"""Turn a Messages API reply into the event stream a ``stream: true`` call gets."""

from __future__ import annotations

import json
from typing import Any

import httpx


def events(reply: dict[str, Any]) -> list[dict[str, Any]]:
    usage = dict(reply.get("usage") or {})
    output = usage.pop("output_tokens", 0)
    start = {
        "type": "message_start",
        "message": {
            "id": reply.get("id", "msg_test"),
            "type": "message",
            "role": "assistant",
            "model": reply.get("model", "claude-haiku-5-5"),
            "content": [],
            "stop_reason": None,
            "usage": {**usage, "output_tokens": 1},
        },
    }
    out: list[dict[str, Any]] = [start]
    for index, block in enumerate(reply.get("content") or []):
        kind = block.get("type") if isinstance(block, dict) else None
        out.append({"type": "content_block_start", "index": index,
                    "content_block": {"type": kind}})
        if kind == "text":
            out.append({"type": "content_block_delta", "index": index,
                        "delta": {"type": "text_delta", "text": block["text"]}})
        elif kind == "thinking":
            out.append({"type": "content_block_delta", "index": index,
                        "delta": {"type": "thinking_delta", "thinking": "hmm"}})
        out.append({"type": "content_block_stop", "index": index})
    out.append({"type": "message_delta",
                "delta": {"stop_reason": reply.get("stop_reason")},
                "usage": {"output_tokens": output}})
    out.append({"type": "message_stop"})
    return out


def body(items: list[dict[str, Any]]) -> str:
    return "".join(
        f"event: {item['type']}\ndata: {json.dumps(item)}\n\n" for item in items
    )


def response(reply: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        text=body(events(reply)),
        headers={"content-type": "text/event-stream"},
    )


def respond(request: httpx.Request, reply: dict[str, Any]) -> httpx.Response:
    """SSE for a streamed request, the plain JSON reply otherwise."""
    if json.loads(request.content).get("stream"):
        return response(reply)
    return httpx.Response(200, json=reply)
