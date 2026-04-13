"""Convert MCP tool definitions to OpenAI-compatible tool-use format for LM Studio."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_CACHED_SCHEMAS: list[dict] | None = None


def mcp_to_openai(mcp_tool: dict) -> dict:
    """Convert a single MCP tool definition to OpenAI function-calling format."""
    input_schema = mcp_tool.get("inputSchema", {})

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


async def get_tool_schemas(mcp_client, force_refresh: bool = False) -> list[dict]:
    """Get OpenAI-format tool schemas from MCP, with caching."""
    global _CACHED_SCHEMAS

    if _CACHED_SCHEMAS is not None and not force_refresh:
        return _CACHED_SCHEMAS

    try:
        mcp_tools = await mcp_client.list_tools()
        _CACHED_SCHEMAS = [mcp_to_openai(t) for t in mcp_tools]
        logger.info("Loaded %d tool schemas from MCP", len(_CACHED_SCHEMAS))
        return _CACHED_SCHEMAS
    except Exception as e:
        logger.warning("Failed to load tool schemas: %s", e)
        return _CACHED_SCHEMAS or []


def clear_cache() -> None:
    """Clear the cached schemas (e.g., on MCP reconnect)."""
    global _CACHED_SCHEMAS
    _CACHED_SCHEMAS = None


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
                    "query": {"type": "string", "description": "What to search for in memory"},
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


def get_builtin_tools() -> list[dict]:
    return list(BUILTIN_TOOLS)
