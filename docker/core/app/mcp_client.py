"""HTTP client for invoking aichat MCP tools."""

from __future__ import annotations

import logging
import os
import uuid

import httpx

logger = logging.getLogger(__name__)

MCP_URL = os.environ.get("MCP_URL", "http://aichat-mcp:8096")


class MCPClient:
    """Invoke MCP tools via the aichat-mcp JSON-RPC gateway."""

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._session_id: str | None = None

    async def init(self) -> None:
        self._http = httpx.AsyncClient(timeout=120.0)
        await self._initialize_session()

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()

    async def _initialize_session(self) -> None:
        """Initialize MCP session to get session ID."""
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
                            "version": "0.1.0",
                        },
                    },
                },
            )
            self._session_id = r.headers.get("Mcp-Session-Id")
            logger.info("MCP session initialized: %s", self._session_id)
        except httpx.HTTPError as e:
            logger.warning("MCP init failed (will retry on first tool call): %s", e)

    async def invoke_tool(self, tool_name: str, params: dict) -> dict:
        """Invoke an MCP tool and return the result."""
        if not self._http:
            raise RuntimeError("MCPClient not initialized")

        headers = {}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        request_id = str(uuid.uuid4())
        r = await self._http.post(
            f"{MCP_URL}/mcp",
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": params,
                },
            },
            headers=headers,
        )
        r.raise_for_status()
        data = r.json()

        if "error" in data:
            logger.error("MCP tool error: %s", data["error"])
            return {"error": data["error"].get("message", "Tool invocation failed")}

        return data.get("result", {})

    async def list_tools(self) -> list[dict]:
        """List available MCP tools."""
        headers = {}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

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
