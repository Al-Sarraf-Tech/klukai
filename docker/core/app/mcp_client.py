"""Klukai's external MCP tools: only the servers named in KLUKAI_MCP_CONFIG.

Her own tools (recall_memory, get_current_time) are built in
(tool_schemas.BUILTIN_TOOLS) and need no server. Until 2026-10 an unset config
fell back to the aichat stack's MCP gateway (aichat-mcp); that fallback is gone,
so without a config core simply offers no external tools.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .mcp_transport import ConfiguredMCP

logger = logging.getLogger(__name__)


class MCPClient:
    """Invoke the allowlisted tools of the configured MCP servers."""

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._configured: ConfiguredMCP | None = None
        # Compose uses an empty value for "no external tools"; a nonempty path
        # opts in and must fail closed when missing or invalid.
        self._config_path = os.environ.get("KLUKAI_MCP_CONFIG") or None

    async def init(self) -> None:
        self._http = httpx.AsyncClient(timeout=120.0)
        if self._config_path is None:
            logger.info("No KLUKAI_MCP_CONFIG: external MCP tools are off")
            return
        from .mcp_transport import ConfiguredMCP, load_config

        self._configured = ConfiguredMCP(load_config(self._config_path), self._http)
        await self._initialize_session()

    async def _initialize_session(self) -> None:
        """(Re)connect the configured servers; the agent loop calls this to
        recover after an empty catalog or a tool timeout. Without a
        configuration there is nothing to connect."""
        if self._configured is not None:
            await self._configured.initialize()

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()

    async def invoke_tool(
        self, tool_name: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Invoke an MCP tool and return the result."""
        if not self._http:
            raise RuntimeError("MCPClient not initialized")
        if self._configured is None:
            return {"error": "MCP configured tools unavailable"}
        return await self._configured.invoke_tool(tool_name, params)

    async def list_tools(self) -> list[dict[str, Any]]:
        """List available MCP tools."""
        if self._configured is None:
            return []
        return await self._configured.list_tools()
