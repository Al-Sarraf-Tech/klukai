"""Tests for app.mcp_client — MCP JSON-RPC HTTP client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_client import MCPClient


def _mk_resp(json_body, headers=None, status_code=200):
    """Build a mock httpx.Response."""
    r = MagicMock()
    r.json = MagicMock(return_value=json_body)
    r.headers = headers or {}
    r.status_code = status_code
    r.raise_for_status = MagicMock()
    return r


class TestInit:
    @pytest.mark.asyncio
    async def test_init_creates_http_client(self):
        c = MCPClient()
        # Patch _initialize_session so init() doesn't try real HTTP
        with patch.object(c, "_initialize_session", new=AsyncMock()):
            await c.init()
        assert c._http is not None
        await c.close()

    @pytest.mark.asyncio
    async def test_close_closes_http_client(self):
        c = MCPClient()
        with patch.object(c, "_initialize_session", new=AsyncMock()):
            await c.init()
        http = c._http
        await c.close()
        # After close, can call again safely
        await c.close()

    @pytest.mark.asyncio
    async def test_initialize_session_captures_session_id(self):
        c = MCPClient()
        c._http = AsyncMock()
        c._http.post = AsyncMock(return_value=_mk_resp(
            {"result": {}},
            headers={"Mcp-Session-Id": "session-xyz"},
        ))
        await c._initialize_session()
        assert c._session_id == "session-xyz"

    @pytest.mark.asyncio
    async def test_initialize_session_fail_soft_on_http_error(self):
        import httpx
        c = MCPClient()
        c._http = AsyncMock()
        c._http.post = AsyncMock(side_effect=httpx.HTTPError("conn refused"))
        # Should not raise
        await c._initialize_session()
        # Session ID stays None on failure
        assert c._session_id is None


class TestInvokeTool:
    @pytest.mark.asyncio
    async def test_raises_when_not_initialized(self):
        c = MCPClient()
        # No init() called → _http is None
        with pytest.raises(RuntimeError, match="not initialized"):
            await c.invoke_tool("any", {})

    @pytest.mark.asyncio
    async def test_returns_result_on_success(self):
        c = MCPClient()
        c._http = AsyncMock()
        c._http.post = AsyncMock(return_value=_mk_resp(
            {"result": {"output": "ok"}}
        ))
        result = await c.invoke_tool("search", {"query": "x"})
        assert result == {"output": "ok"}

    @pytest.mark.asyncio
    async def test_returns_error_on_jsonrpc_error(self):
        c = MCPClient()
        c._http = AsyncMock()
        c._http.post = AsyncMock(return_value=_mk_resp(
            {"error": {"code": -32601, "message": "tool not found"}}
        ))
        result = await c.invoke_tool("nonexistent", {})
        assert "error" in result
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_includes_session_header_when_present(self):
        c = MCPClient()
        c._http = AsyncMock()
        c._session_id = "test-session-id"
        c._http.post = AsyncMock(return_value=_mk_resp({"result": {}}))
        await c.invoke_tool("any", {})
        # Check the headers passed to post
        call_kwargs = c._http.post.call_args.kwargs
        assert call_kwargs["headers"].get("Mcp-Session-Id") == "test-session-id"

    @pytest.mark.asyncio
    async def test_no_session_header_when_id_none(self):
        c = MCPClient()
        c._http = AsyncMock()
        c._session_id = None
        c._http.post = AsyncMock(return_value=_mk_resp({"result": {}}))
        await c.invoke_tool("any", {})
        call_kwargs = c._http.post.call_args.kwargs
        # Empty headers dict — no session id
        assert "Mcp-Session-Id" not in call_kwargs.get("headers", {})


class TestListTools:
    @pytest.mark.asyncio
    async def test_returns_tools_list(self):
        c = MCPClient()
        c._http = AsyncMock()
        c._http.post = AsyncMock(return_value=_mk_resp(
            {"result": {"tools": [{"name": "alpha"}, {"name": "beta"}]}}
        ))
        tools = await c.list_tools()
        assert len(tools) == 2
        assert tools[0]["name"] == "alpha"

    @pytest.mark.asyncio
    async def test_empty_when_no_tools(self):
        c = MCPClient()
        c._http = AsyncMock()
        c._http.post = AsyncMock(return_value=_mk_resp({"result": {}}))
        tools = await c.list_tools()
        assert tools == []
