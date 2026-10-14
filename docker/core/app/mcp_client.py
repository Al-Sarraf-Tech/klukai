"""HTTP client for invoking aichat MCP tools."""

from __future__ import annotations

import logging
import os
import uuid
from typing import TYPE_CHECKING

import httpx

from . import __version__

if TYPE_CHECKING:
    from .mcp_transport import ConfiguredMCP

logger = logging.getLogger(__name__)

MCP_URL = os.environ.get("MCP_URL", "http://aichat-mcp:8096")


class MCPClient:
    """Invoke MCP tools via the aichat-mcp JSON-RPC gateway."""

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._configured: ConfiguredMCP | None = None
        # Compose uses an empty value for the legacy default; a nonempty path
        # opts in and must fail closed when missing or invalid.
        self._config_path = os.environ.get("KLUKAI_MCP_CONFIG") or None

    async def init(self) -> None:
        self._http = httpx.AsyncClient(timeout=120.0)
        if self._config_path is not None:
            from .mcp_transport import ConfiguredMCP, load_config

            self._configured = ConfiguredMCP(load_config(self._config_path), self._http)
        await self._initialize_session()

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()

    async def _initialize_session(self) -> None:
        """Initialize MCP session to get session ID."""
        if self._config_path is not None:
            if self._configured is not None:
                await self._configured.initialize()
            return
        try:
            r = await self._http.post(
                f"{MCP_URL}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": str(uuid.uuid4()),
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "companion-core",
                            "version": __version__,
                        },
                    },
                },
            )
            self._session_id = r.headers.get("Mcp-Session-Id")
            logger.info("MCP session initialized: %s", self._session_id)
        except httpx.HTTPError as e:
            logger.warning("MCP init failed (will retry on first tool call): %s", e)

    def _session_headers(self) -> dict[str, str]:
        headers = {}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def invoke_tool(self, tool_name: str, params: dict) -> dict:
        """Invoke an MCP tool and return the result."""
        if not self._http:
            raise RuntimeError("MCPClient not initialized")
        if self._config_path is not None:
            if self._configured is None:
                return {"error": "MCP configured tools unavailable"}
            return await self._configured.invoke_tool(tool_name, params)

        if self._session_id is None:
            # Boot-time init fails whenever aichat-mcp is still starting; the
            # warning above promised a retry here, but nothing ever made one,
            # so every tool call went out sessionless for the life of the
            # process.
            await self._initialize_session()

        request_id = str(uuid.uuid4())
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": params,
            },
        }
        had_session = self._session_id is not None
        r = await self._http.post(
            f"{MCP_URL}/mcp", json=body, headers=self._session_headers()
        )
        if had_session and r.status_code in (400, 404):
            # The gateway no longer knows our session (restart or expiry).
            # Re-initialize once and retry the call once with the new id.
            logger.warning(
                "MCP rejected session (HTTP %s); re-initializing", r.status_code
            )
            self._session_id = None
            await self._initialize_session()
            r = await self._http.post(
                f"{MCP_URL}/mcp", json=body, headers=self._session_headers()
            )
        r.raise_for_status()
        data = r.json()

        if "error" in data:
            logger.error("MCP tool error: %s", data["error"])
            return {"error": data["error"].get("message", "Tool invocation failed")}

        return data.get("result", {})

    async def list_tools(self) -> list[dict]:
        """List available MCP tools."""
        if self._config_path is not None:
            if self._configured is None:
                return []
            return await self._configured.list_tools()
        headers = self._session_headers()

        r = await self._http.post(
            f"{MCP_URL}/mcp",
            json={
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "tools/list",
                "params": {},
            },
            headers=headers,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("result", {}).get("tools", [])
