"""Behavioral coverage for app.mcp_client — MCP JSON-RPC protocol client.

The aichat-mcp gateway is mocked via an injected fake httpx.AsyncClient that
records every POST. We assert the JSON-RPC envelopes (method, params, headers),
session-id propagation, tool-result extraction, error handling, and the
uninitialized guard. No network.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app import mcp_client
from app.mcp_client import MCPClient


class _Resp:
    def __init__(self, json_data=None, headers=None, raise_exc=None):
        self._json = json_data if json_data is not None else {}
        self.headers = headers or {}
        self._raise_exc = raise_exc

    def json(self):
        return self._json

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc


def _client_with_post(post_mock, session_id=None):
    """Build an MCPClient with a fake http client wired to post_mock."""
    c = MCPClient()
    http = MagicMock()
    http.post = post_mock
    http.aclose = AsyncMock()
    c._http = http
    c._session_id = session_id
    return c


# ── init / session lifecycle ─────────────────────────────────────────────────

class TestInit:
    @pytest.mark.asyncio
    async def test_init_creates_client_and_captures_session_id(self):
        resp = _Resp(json_data={}, headers={"Mcp-Session-Id": "sess-abc"})
        fake_http = MagicMock()
        fake_http.post = AsyncMock(return_value=resp)
        with patch("httpx.AsyncClient", return_value=fake_http):
            c = MCPClient()
            await c.init()
        assert c._http is fake_http
        assert c._session_id == "sess-abc"
        # The initialize request used the MCP "initialize" method.
        body = fake_http.post.call_args.kwargs["json"]
        assert body["method"] == "initialize"
        assert body["params"]["clientInfo"]["name"] == "companion-core"
        assert body["jsonrpc"] == "2.0"

    @pytest.mark.asyncio
    async def test_initialize_session_swallows_http_error(self):
        fake_http = MagicMock()
        fake_http.post = AsyncMock(side_effect=httpx.ConnectError("mcp down"))
        with patch("httpx.AsyncClient", return_value=fake_http):
            c = MCPClient()
            await c.init()  # must not raise
        assert c._session_id is None  # never set when init fails

    @pytest.mark.asyncio
    async def test_close_aclose_when_present(self):
        c = MCPClient()
        c._http = MagicMock()
        c._http.aclose = AsyncMock()
        await c.close()
        c._http.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_noop_when_no_client(self):
        c = MCPClient()
        assert c._http is None
        await c.close()  # must not raise


# ── invoke_tool ──────────────────────────────────────────────────────────────

class TestInvokeTool:
    @pytest.mark.asyncio
    async def test_raises_when_not_initialized(self):
        c = MCPClient()
        with pytest.raises(RuntimeError, match="not initialized"):
            await c.invoke_tool("any", {})

    @pytest.mark.asyncio
    async def test_returns_result_and_sends_tools_call_envelope(self):
        post = AsyncMock(return_value=_Resp(json_data={"result": {"output": 42}}))
        c = _client_with_post(post, session_id="sess-1")
        out = await c.invoke_tool("calc", {"x": 1})
        assert out == {"output": 42}
        body = post.call_args.kwargs["json"]
        assert body["method"] == "tools/call"
        assert body["params"] == {"name": "calc", "arguments": {"x": 1}}
        # Session id must be forwarded as a header when present.
        assert post.call_args.kwargs["headers"]["Mcp-Session-Id"] == "sess-1"

    @pytest.mark.asyncio
    async def test_no_session_id_sends_empty_headers(self):
        post = AsyncMock(return_value=_Resp(json_data={"result": {}}))
        c = _client_with_post(post, session_id=None)
        await c.invoke_tool("noop", {})
        assert post.call_args.kwargs["headers"] == {}

    @pytest.mark.asyncio
    async def test_error_response_returns_error_message(self):
        post = AsyncMock(return_value=_Resp(
            json_data={"error": {"code": -32601, "message": "no such tool"}}))
        c = _client_with_post(post, session_id="s")
        out = await c.invoke_tool("ghost", {})
        assert out == {"error": "no such tool"}

    @pytest.mark.asyncio
    async def test_error_response_without_message_uses_default(self):
        post = AsyncMock(return_value=_Resp(json_data={"error": {"code": -1}}))
        c = _client_with_post(post, session_id="s")
        out = await c.invoke_tool("ghost", {})
        assert out == {"error": "Tool invocation failed"}

    @pytest.mark.asyncio
    async def test_missing_result_returns_empty_dict(self):
        post = AsyncMock(return_value=_Resp(json_data={}))
        c = _client_with_post(post, session_id="s")
        out = await c.invoke_tool("t", {})
        assert out == {}

    @pytest.mark.asyncio
    async def test_http_status_error_propagates(self):
        err = httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())
        post = AsyncMock(return_value=_Resp(raise_exc=err))
        c = _client_with_post(post, session_id="s")
        with pytest.raises(httpx.HTTPStatusError):
            await c.invoke_tool("t", {})


# ── list_tools ───────────────────────────────────────────────────────────────

class TestListTools:
    @pytest.mark.asyncio
    async def test_returns_tools_list_with_session_header(self):
        tools = [{"name": "calc"}, {"name": "search"}]
        post = AsyncMock(return_value=_Resp(json_data={"result": {"tools": tools}}))
        c = _client_with_post(post, session_id="sess-list")
        out = await c.list_tools()
        assert out == tools
        body = post.call_args.kwargs["json"]
        assert body["method"] == "tools/list"
        assert post.call_args.kwargs["headers"]["Mcp-Session-Id"] == "sess-list"

    @pytest.mark.asyncio
    async def test_no_session_id_sends_empty_headers(self):
        """Covers the falsy-session branch (line 91)."""
        post = AsyncMock(return_value=_Resp(json_data={"result": {"tools": []}}))
        c = _client_with_post(post, session_id=None)
        out = await c.list_tools()
        assert out == []
        assert post.call_args.kwargs["headers"] == {}

    @pytest.mark.asyncio
    async def test_missing_tools_key_returns_empty(self):
        post = AsyncMock(return_value=_Resp(json_data={"result": {}}))
        c = _client_with_post(post, session_id="s")
        assert await c.list_tools() == []

    @pytest.mark.asyncio
    async def test_http_error_propagates(self):
        err = httpx.HTTPStatusError("503", request=MagicMock(), response=MagicMock())
        post = AsyncMock(return_value=_Resp(raise_exc=err))
        c = _client_with_post(post, session_id="s")
        with pytest.raises(httpx.HTTPStatusError):
            await c.list_tools()


class TestModuleConfig:
    def test_mcp_url_defaults_to_gateway(self):
        # Default points at the in-cluster aichat-mcp gateway.
        assert mcp_client.MCP_URL.startswith("http")
