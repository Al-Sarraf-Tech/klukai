"""Convert MCP tool definitions to OpenAI-compatible tool-use format for LM Studio."""

from __future__ import annotations
from typing import Any


import logging
import time
from copy import deepcopy
from weakref import WeakKeyDictionary

logger = logging.getLogger(__name__)

SCHEMA_CACHE_SECONDS = 60.0
_CACHED_SCHEMAS: WeakKeyDictionary[object, tuple[float, list[dict[str, Any]]]] = (
    WeakKeyDictionary()
)


def mcp_to_openai(mcp_tool: dict[str, Any]) -> dict[str, Any]:
    """Convert a single MCP tool definition to OpenAI function-calling format."""
    input_schema = deepcopy(mcp_tool.get("inputSchema", {}))

    # Ensure required top-level fields
    if "type" not in input_schema:
        input_schema["type"] = "object"
    if "properties" not in input_schema:
        input_schema["properties"] = {}

    return {
        "type": "function",
        "function": {
            "name": mcp_tool["name"],
            "description": mcp_tool.get("description", ""),
            "parameters": input_schema,
        },
    }


async def get_tool_schemas(
    mcp_client: Any, force_refresh: bool = False
) -> list[dict[str, Any]]:
    """Cache successful discovery per client briefly; empty catalogs retry."""
    cached = _CACHED_SCHEMAS.get(mcp_client)
    if (
        cached
        and not force_refresh
        and time.monotonic() - cached[0] < SCHEMA_CACHE_SECONDS
    ):
        return cached[1]

    try:
        mcp_tools = await mcp_client.list_tools()
        schemas = [mcp_to_openai(t) for t in mcp_tools]
        if schemas:
            _CACHED_SCHEMAS[mcp_client] = (time.monotonic(), schemas)
        else:
            _CACHED_SCHEMAS.pop(mcp_client, None)
        logger.info("Loaded %d tool schemas from MCP", len(schemas))
        return schemas
    except Exception as e:
        logger.warning("Failed to load tool schemas: %s", e)
        return cached[1] if cached else []


def clear_cache() -> None:
    """Clear the cached schemas (e.g., on MCP reconnect)."""
    _CACHED_SCHEMAS.clear()


# Built-in tools that don't need MCP
BUILTIN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": "Search Klukai's memory for facts, past conversations, and details about the Commander. Use when asked 'do you remember', 'what did I tell you', 'my favorite', etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for in memory",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current date and time. Use when the conversation references time of day, scheduling, or current date.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def get_builtin_tools() -> list[dict[str, Any]]:
    return list(BUILTIN_TOOLS)
