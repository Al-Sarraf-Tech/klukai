"""Unit tests for app.tool_schemas — MCP → OpenAI tool-format converter + cache."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app import tool_schemas


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    tool_schemas.clear_cache()
    yield
    tool_schemas.clear_cache()


class TestMcpToOpenAI:
    def test_minimal_mcp_tool_gets_object_defaults(self):
        result = tool_schemas.mcp_to_openai({"name": "ping", "description": "ping it"})
        assert result == {
            "type": "function",
            "function": {
                "name": "ping",
                "description": "ping it",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    def test_preserves_input_schema(self):
        mcp_tool = {
            "name": "search",
            "description": "search for things",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
        result = tool_schemas.mcp_to_openai(mcp_tool)
        assert result["function"]["parameters"]["required"] == ["query"]
        assert result["function"]["parameters"]["properties"]["query"]["type"] == "string"

    def test_missing_description_defaults_to_empty(self):
        result = tool_schemas.mcp_to_openai({"name": "ping"})
        assert result["function"]["description"] == ""

    def test_schema_without_type_gets_object(self):
        mcp_tool = {"name": "x", "inputSchema": {"properties": {}}}
        result = tool_schemas.mcp_to_openai(mcp_tool)
        assert result["function"]["parameters"]["type"] == "object"

    def test_schema_without_properties_gets_empty_dict(self):
        mcp_tool = {"name": "x", "inputSchema": {"type": "object"}}
        result = tool_schemas.mcp_to_openai(mcp_tool)
        assert result["function"]["parameters"]["properties"] == {}


class TestGetToolSchemas:
    @pytest.mark.asyncio
    async def test_fetches_and_caches(self):
        client = AsyncMock()
        client.list_tools = AsyncMock(return_value=[
            {"name": "alpha", "description": "a"},
            {"name": "beta", "description": "b"},
        ])

        first = await tool_schemas.get_tool_schemas(client)
        assert len(first) == 2
        assert first[0]["function"]["name"] == "alpha"

        # Second call should NOT hit the client again
        client.list_tools.reset_mock()
        second = await tool_schemas.get_tool_schemas(client)
        assert second == first
        assert client.list_tools.call_count == 0

    @pytest.mark.asyncio
    async def test_force_refresh_bypasses_cache(self):
        client = AsyncMock()
        client.list_tools = AsyncMock(return_value=[{"name": "x"}])

        await tool_schemas.get_tool_schemas(client)
        client.list_tools.reset_mock()

        await tool_schemas.get_tool_schemas(client, force_refresh=True)
        assert client.list_tools.call_count == 1

    @pytest.mark.asyncio
    async def test_failure_returns_empty_when_no_cache(self):
        client = AsyncMock()
        client.list_tools = AsyncMock(side_effect=RuntimeError("MCP down"))

        result = await tool_schemas.get_tool_schemas(client)
        assert result == []

    @pytest.mark.asyncio
    async def test_failure_returns_cached_when_available(self):
        # Populate cache first
        good_client = AsyncMock()
        good_client.list_tools = AsyncMock(return_value=[{"name": "ok"}])
        cached = await tool_schemas.get_tool_schemas(good_client)

        # Now break the client and force refresh
        bad_client = AsyncMock()
        bad_client.list_tools = AsyncMock(side_effect=RuntimeError("MCP down"))
        result = await tool_schemas.get_tool_schemas(bad_client, force_refresh=True)

        # Should return the previously-cached schemas, not empty
        assert result == cached


class TestClearCache:
    @pytest.mark.asyncio
    async def test_drops_cache(self):
        client = AsyncMock()
        client.list_tools = AsyncMock(return_value=[{"name": "x"}])

        await tool_schemas.get_tool_schemas(client)
        assert tool_schemas._CACHED_SCHEMAS is not None

        tool_schemas.clear_cache()
        assert tool_schemas._CACHED_SCHEMAS is None


class TestBuiltinTools:
    def test_returns_list_copy(self):
        tools = tool_schemas.get_builtin_tools()
        assert isinstance(tools, list)
        # Mutating the returned list should not affect the module's BUILTIN_TOOLS
        tools.append({"junk": True})
        assert tool_schemas.get_builtin_tools() != tools

    def test_includes_recall_memory(self):
        names = [t["function"]["name"] for t in tool_schemas.get_builtin_tools()]
        assert "recall_memory" in names

    def test_includes_get_current_time(self):
        names = [t["function"]["name"] for t in tool_schemas.get_builtin_tools()]
        assert "get_current_time" in names

    def test_recall_memory_requires_query(self):
        recall = next(
            t for t in tool_schemas.get_builtin_tools()
            if t["function"]["name"] == "recall_memory"
        )
        assert "query" in recall["function"]["parameters"]["required"]
