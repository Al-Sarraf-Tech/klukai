"""MCPClient lifecycle and its no-configuration behaviour.

Configured servers (policy, framing, allowlists) are covered by
test_mcp_client_configured.py. Without KLUKAI_MCP_CONFIG core offers no
external tools and never opens a connection; the built-in tools
(recall_memory, get_current_time) do not go through MCPClient at all.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import mcp_client
from app.mcp_client import MCPClient


@pytest.fixture
def unconfigured(monkeypatch):
    monkeypatch.delenv("KLUKAI_MCP_CONFIG", raising=False)
    return MCPClient()


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_init_creates_http_client_without_any_request(self, unconfigured):
        fake = MagicMock()
        with patch("app.mcp_client.httpx.AsyncClient", return_value=fake):
            await unconfigured.init()
        assert unconfigured._http is fake
        fake.post.assert_not_called()
        fake.get.assert_not_called()
        fake.stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_closes_http_client(self, unconfigured):
        unconfigured._http = MagicMock()
        unconfigured._http.aclose = AsyncMock()
        await unconfigured.close()
        unconfigured._http.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_noop_when_never_initialized(self, unconfigured):
        await unconfigured.close()

    @pytest.mark.asyncio
    async def test_bad_configuration_disables_external_tools(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "mcp.json"
        path.write_text("{not json")
        monkeypatch.setenv("KLUKAI_MCP_CONFIG", str(path))
        client = MCPClient()
        fake = MagicMock()
        with patch("app.mcp_client.httpx.AsyncClient", return_value=fake):
            await client.init()
        assert await client.list_tools() == []
        fake.post.assert_not_called()
        fake.stream.assert_not_called()


class TestWithoutConfiguration:
    @pytest.mark.asyncio
    async def test_invoke_raises_when_not_initialized(self, unconfigured):
        with pytest.raises(RuntimeError):
            await unconfigured.invoke_tool("anything", {})

    @pytest.mark.asyncio
    async def test_invoke_reports_tools_unavailable(self, unconfigured):
        unconfigured._http = MagicMock()
        assert await unconfigured.invoke_tool("web_search", {"q": "x"}) == {
            "error": "MCP configured tools unavailable"
        }
        unconfigured._http.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_lists_no_tools(self, unconfigured):
        assert await unconfigured.list_tools() == []

    def test_no_aichat_gateway_remains(self):
        assert not hasattr(mcp_client, "MCP_URL")
        assert "aichat-mcp:8096" not in open(mcp_client.__file__).read()


class TestRecovery:
    @pytest.mark.asyncio
    async def test_reinitialize_without_configuration_is_a_no_op(self, unconfigured):
        unconfigured._http = MagicMock()
        await unconfigured._initialize_session()
        unconfigured._http.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_reinitialize_reconnects_configured_servers(self, unconfigured):
        unconfigured._configured = MagicMock()
        unconfigured._configured.initialize = AsyncMock()
        await unconfigured._initialize_session()
        unconfigured._configured.initialize.assert_awaited_once()
