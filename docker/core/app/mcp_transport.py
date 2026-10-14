"""Allowlisted Streamable HTTP connections for the opt-in MCP configuration.

Errors deliberately contain no remote text, URLs, headers, or credential paths.
The legacy gateway protocol lives in mcp_client and remains independent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import __version__

logger = logging.getLogger(__name__)

_PROTOCOL = "2025-03-26"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_PAGES = 20
_DISCOVERY_TIMEOUT = 10.0
_TOOL_TIMEOUT = 45.0
_CONNECT_TIMEOUT = 5.0
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class MCPFailure(Exception):
    """A safe local error; remote error bodies are never copied here."""


class _ExpiredSession(MCPFailure):
    pass


@dataclass(frozen=True)
class ServerConfig:
    name: str
    url: str
    allowed_tools: frozenset[str]
    enabled_env: str | None = None
    token_env: str | None = None
    token_file: str | None = None

    def enabled(self) -> bool:
        return self.enabled_env is None or os.getenv(self.enabled_env, "").strip().lower() in {"true", "1", "yes"}


def load_config(path: str) -> list[ServerConfig]:
    """Validate all policy before making any connection; invalid means no tools."""
    from .tool_schemas import get_builtin_tools

    reserved = {tool["function"]["name"] for tool in get_builtin_tools()}
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"servers"} or not isinstance(data["servers"], list):
            raise ValueError
        if len(data["servers"]) > 16:
            raise ValueError
        names, tools = set(), set()
        configs = []
        for entry in data["servers"]:
            if not isinstance(entry, dict) or set(entry) - {"name", "url", "allowed_tools", "enabled_env", "token_env", "token_file"}:
                raise ValueError
            name, url, allowed = entry.get("name"), entry.get("url"), entry.get("allowed_tools")
            if not isinstance(name, str) or not _NAME.fullmatch(name) or name in names:
                raise ValueError
            if not isinstance(url, str):
                raise ValueError
            parsed = urlsplit(url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.query or parsed.fragment):
                raise ValueError
            # Also reject malformed ports before httpx can render a URL error.
            _ = parsed.port
            if not isinstance(allowed, list) or not allowed:
                raise ValueError
            for tool in allowed:
                if not isinstance(tool, str) or not _NAME.fullmatch(tool) or tool in tools or tool in reserved:
                    raise ValueError
                tools.add(tool)
            for field in ("enabled_env", "token_env"):
                if field in entry and (not isinstance(entry[field], str) or not _ENV_NAME.fullmatch(entry[field])):
                    raise ValueError
            if "token_file" in entry and (not isinstance(entry["token_file"], str) or not entry["token_file"]):
                raise ValueError
            if "token_file" in entry and "token_env" in entry:
                raise ValueError
            names.add(name)
            configs.append(ServerConfig(name, url, frozenset(allowed), entry.get("enabled_env"),
                                        entry.get("token_env"), entry.get("token_file")))
        return configs
    except (OSError, ValueError, TypeError, RecursionError):
        logger.warning("MCP configuration rejected; configured tools disabled")
        return []


def _matching_response(data: object, request_id: str) -> dict | None:
    messages = data if isinstance(data, list) else [data]
    for message in messages:
        if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
                or message.get("id") != request_id or "method" in message):
            continue
        if "error" in message:
            raise MCPFailure("MCP request rejected")
        result = message.get("result")
        if not isinstance(result, dict):
            raise MCPFailure("Invalid MCP response")
        return result
    return None


async def _read_response(response: httpx.Response, request_id: str) -> dict:
    """Find the response to this request, bounding bytes even in an SSE stream."""
    is_sse = response.headers.get("content-type", "").split(";", 1)[0].strip().lower() == "text/event-stream"
    if not is_sse and response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise MCPFailure("Unsupported MCP response")
    pending = bytearray()
    event_data = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise MCPFailure("MCP response too large")
        pending.extend(chunk)
        if not is_sse:
            continue
        while b"\n" in pending:
            line, _, remainder = pending.partition(b"\n")
            pending = bytearray(remainder)
            line = line.rstrip(b"\r")
            if line.startswith(b"data:"):
                event_data.append(line[5:].removeprefix(b" "))
            elif not line and event_data:
                result = _matching_response(json.loads(b"\n".join(event_data)), request_id)
                event_data = []
                if result is not None:
                    return result
    if is_sse:
        if pending.startswith(b"data:"):
            event_data.append(pending[5:].removeprefix(b" ").rstrip(b"\r"))
        if event_data:
            result = _matching_response(json.loads(b"\n".join(event_data)), request_id)
            if result is not None:
                return result
    else:
        result = _matching_response(json.loads(pending), request_id)
        if result is not None:
            return result
    raise MCPFailure("Missing MCP response")


class MCPConnection:
    """One configured server with independent initialization and session state."""

    def __init__(self, config: ServerConfig, http: httpx.AsyncClient):
        self.config = config
        self._http = http
        self._session_id: str | None = None
        self._initialized = False
        self._init_lock = asyncio.Lock()

    def _headers(self) -> dict[str, str]:
        if not self.config.enabled():
            raise MCPFailure("MCP server disabled")
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": _PROTOCOL}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self.config.token_file:
            with Path(self.config.token_file).open("r", encoding="utf-8") as stream:
                token = stream.read(8193).strip()
        elif self.config.token_env:
            token = os.getenv(self.config.token_env, "").strip()
        else:
            return headers
        if not token or len(token) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise MCPFailure("MCP credential unavailable")
        headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _post(self, body: dict) -> dict:
        headers = self._headers()
        deadline = _TOOL_TIMEOUT if body["method"] == "tools/call" else _DISCOVERY_TIMEOUT
        timeout = httpx.Timeout(deadline, connect=min(_CONNECT_TIMEOUT, deadline))
        # httpx bounds individual network operations; the outer deadline also
        # bounds a peer that keeps its SSE stream alive without a final result.
        async with asyncio.timeout(deadline):
            async with self._http.stream("POST", self.config.url, json=body, headers=headers, timeout=timeout) as response:
                if response.status_code == 404 and "Mcp-Session-Id" in headers:
                    raise _ExpiredSession("MCP session expired")
                if not 200 <= response.status_code < 300:
                    raise MCPFailure("MCP HTTP request failed")
                if "id" not in body:
                    if response.status_code != 202:
                        raise MCPFailure("MCP initialization not accepted")
                    return {}
                result = await _read_response(response, body["id"])
                # Even a faulty peer echoing request headers must not disclose
                # bearer credentials or session credentials to model content.
                encoded = json.dumps(result)
                credentials = [headers.get("Authorization", "").removeprefix("Bearer "),
                               headers.get("Mcp-Session-Id", "")]
                if any(value and json.dumps(value)[1:-1] in encoded for value in credentials):
                    raise MCPFailure("Sensitive MCP response rejected")
                if body["method"] == "initialize":
                    session = response.headers.get("Mcp-Session-Id")
                    if session is not None and (not session or len(session) > 8192
                            or any(ord(c) < 33 or ord(c) > 126 for c in session)):
                        raise MCPFailure("Invalid MCP session")
                    self._session_id = session
                return result

    async def initialize(self) -> None:
        async with self._init_lock:
            if self._initialized:
                return
            self._session_id = None
            try:
                result = await self._post({"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "initialize",
                    "params": {"protocolVersion": _PROTOCOL, "capabilities": {},
                               "clientInfo": {"name": "companion-core", "version": __version__}}})
                if result.get("protocolVersion") != _PROTOCOL:
                    raise MCPFailure("Unsupported MCP protocol")
                await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
                self._initialized = True
            except Exception:
                self._session_id = None
                raise

    async def request(self, method: str, params: dict) -> dict:
        await self.initialize()
        body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        try:
            return await self._post(body)
        except _ExpiredSession:
            # HTTP 404 with a session is the protocol's explicit rejection.
            # Never replay on a timeout, generic 400, RPC error or HTTP 5xx:
            # the operation may already have executed and incurred a charge.
            self._initialized = False
            self._session_id = None
            await self.initialize()
            return await self._post(body)

    async def list_tools(self) -> list[dict]:
        tools, names, cursors = [], set(), set()
        params: dict[str, str] = {}
        for _ in range(_MAX_PAGES):
            result = await self.request("tools/list", params)
            page = result.get("tools")
            if not isinstance(page, list):
                raise MCPFailure("Invalid MCP catalog")
            for tool in page:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                    raise MCPFailure("Invalid MCP tool")
                name = tool["name"]
                if name not in self.config.allowed_tools:
                    continue
                if name in names:
                    raise MCPFailure("Duplicate MCP tool")
                schema = tool.get("inputSchema")
                if (not isinstance(schema, dict) or schema.get("type") != "object"
                        or not isinstance(tool.get("description", ""), str)):
                    raise MCPFailure("Invalid MCP tool schema")
                names.add(name)
                tools.append({"name": name, "description": tool.get("description", ""), "inputSchema": schema})
            cursor = result.get("nextCursor")
            if cursor is None:
                return tools
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise MCPFailure("Invalid MCP pagination")
            cursors.add(cursor)
            params = {"cursor": cursor}
        raise MCPFailure("MCP pagination limit exceeded")


class ConfiguredMCP:
    """Aggregate only successful catalogs; failed servers recover on discovery."""

    def __init__(self, configs: list[ServerConfig], http: httpx.AsyncClient):
        self._connections = [MCPConnection(config, http) for config in configs]
        self._routes: dict[str, MCPConnection] = {}

    async def initialize(self) -> None:
        for connection in self._connections:
            if not connection.config.enabled():
                continue
            try:
                await connection.initialize()
            except Exception:
                logger.warning("MCP initialization unavailable for %s", connection.config.name)

    async def list_tools(self) -> list[dict]:
        routes, tools = {}, []
        for connection in self._connections:
            if not connection.config.enabled():
                continue
            try:
                catalog = await connection.list_tools()
            except Exception:
                logger.warning("MCP discovery unavailable for %s", connection.config.name)
                continue
            tools.extend(catalog)
            routes.update({tool["name"]: connection for tool in catalog})
        self._routes = routes
        return tools

    async def invoke_tool(self, name: str, arguments: dict) -> dict:
        if not isinstance(arguments, dict):
            return {"error": "Tool arguments must be an object"}
        connection = self._routes.get(name) if isinstance(name, str) else None
        if not connection or not connection.config.enabled() or name not in connection.config.allowed_tools:
            return {"error": "Tool is unavailable or not allowed"}
        try:
            result = await connection.request("tools/call", {"name": name, "arguments": arguments})
            if result.get("isError") or "error" in result:
                return {"error": "MCP tool invocation failed"}
            return result
        except Exception:
            logger.warning("MCP tool invocation unavailable for %s", connection.config.name)
            return {"error": "MCP tool invocation failed"}
