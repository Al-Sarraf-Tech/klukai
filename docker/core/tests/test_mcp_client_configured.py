"""Configured MCP policy and real HTTP framing, without any live services."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.mcp_client import MCPClient


def definition(name):
    return {"name": name, "description": "Public information", "inputSchema": {"type": "object"}}


def reply(request, result, *, session=None, sse=False):
    body = json.loads(request.content)
    payload = {"jsonrpc": "2.0", "id": body["id"], "result": result}
    headers = {"Mcp-Session-Id": session} if session else {}
    if sse:
        headers["Content-Type"] = "text/event-stream"
        return httpx.Response(200, headers=headers, content=f"event: message\ndata: {json.dumps(payload)}\n\n")
    return httpx.Response(200, headers=headers, json=payload)


class Server:
    """Wire-level peer: records outbound envelopes, returns protocol responses."""

    def __init__(self, names=("search",), *, sse=False, session="session-1"):
        self.names = names
        self.sse = sse
        self.session = session
        self.requests = []
        self.override = None

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append((request, body))
        if self.override:
            response = self.override(request, body)
            if response is not None:
                return response
        if body["method"] == "initialize":
            return reply(request, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                                   "serverInfo": {"name": "fixture", "version": "1"}},
                         session=self.session, sse=self.sse)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "tools/list":
            return reply(request, {"tools": [definition(n) for n in self.names]}, sse=self.sse)
        return reply(request, {"content": [{"type": "text", "text": "https://example.org/result"}]}, sse=self.sse)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    real_client = httpx.AsyncClient

    def make(server, servers=None, raw=None):
        path = tmp_path / "mcp.json"
        if raw is not None:
            path.write_text(raw)
        else:
            path.write_text(json.dumps({"servers": servers if servers is not None else [
                {"name": "reference", "url": "http://reference/mcp", "allowed_tools": ["search"]}
            ]}))
        monkeypatch.setenv("KLUKAI_MCP_CONFIG", str(path))
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(server), **kw))
        return MCPClient()

    return make


@pytest.mark.asyncio
async def test_empty_environment_offers_no_external_tools(configured, monkeypatch):
    """No config means no external tools, and no request to any server: the
    old fallback to the aichat stack's MCP gateway is gone."""
    server = Server(("legacy_tool",))
    configured(server)
    monkeypatch.setenv("KLUKAI_MCP_CONFIG", "")
    client = MCPClient()
    await client.init()
    assert await client.list_tools() == []
    assert await client.invoke_tool("legacy_tool", {}) == {
        "error": "MCP configured tools unavailable"}
    assert server.requests == []
    await client.close()


@pytest.mark.parametrize("sse", [False, True])
@pytest.mark.asyncio
async def test_handshake_discovery_and_dispatch_share_allowlist(configured, sse):
    server = Server(("search", "delete_everything"), sse=sse)
    client = configured(server)
    await client.init()
    try:
        assert await client.list_tools() == [definition("search")]
        assert await client.invoke_tool("search", {"query": "public facts"}) == {
            "content": [{"type": "text", "text": "https://example.org/result"}]}
        assert [b["method"] for _, b in server.requests] == [
            "initialize", "notifications/initialized", "tools/list", "tools/call"]
        for request, body in server.requests:
            assert request.headers["accept"] == "application/json, text/event-stream"
            if body["method"] != "initialize":
                assert request.headers["Mcp-Session-Id"] == "session-1"
        assert "id" not in server.requests[1][1]
        assert server.requests[0][1]["params"]["protocolVersion"] == "2025-03-26"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sessionless_peer_initializes_once(configured):
    server = Server(session=None)
    client = configured(server)
    await client.init()
    await client.list_tools()
    await client.invoke_tool("search", {})
    await client.list_tools()
    assert [b["method"] for _, b in server.requests].count("initialize") == 1
    await client.close()


@pytest.mark.parametrize("enabled", [None, "false", "0", "no", "garbage"])
@pytest.mark.asyncio
async def test_disabled_servers_cannot_be_discovered_or_invoked(configured, monkeypatch, enabled):
    monkeypatch.delenv("TEST_BRAVE_ENABLED", raising=False)
    if enabled is not None:
        monkeypatch.setenv("TEST_BRAVE_ENABLED", enabled)
    server = Server()
    client = configured(server, [{"name": "brave", "url": "http://brave/mcp", "allowed_tools": ["search"],
                                  "enabled_env": "TEST_BRAVE_ENABLED"}])
    await client.init()
    assert await client.list_tools() == []
    assert "error" in await client.invoke_tool("search", {})
    assert server.requests == []
    await client.close()


@pytest.mark.parametrize("raw", ["broken", "{}", '{"servers": "wrong"}',
    '{"servers": [{"name": "one", "url": "http://user:secret@host/mcp", "allowed_tools": ["search"]}]}',
    '{"servers": [{"name": "one", "url": "http://host/mcp?token=secret", "allowed_tools": ["search"]}]}'])
@pytest.mark.asyncio
async def test_invalid_config_fails_closed_without_legacy_requests(configured, raw, caplog):
    server = Server()
    client = configured(server, raw=raw)
    await client.init()
    assert await client.list_tools() == []
    assert "error" in await client.invoke_tool("search", {})
    assert server.requests == []
    assert "secret" not in caplog.text
    await client.close()


@pytest.mark.parametrize("names", [["search", "search"], ["recall_memory"], ["get_current_time"]])
@pytest.mark.asyncio
async def test_duplicate_and_builtin_names_fail_closed(configured, names):
    server = Server(names)
    client = configured(server, [{"name": "reference", "url": "http://reference/mcp", "allowed_tools": names}])
    await client.init()
    assert await client.list_tools() == []
    assert server.requests == []
    await client.close()


@pytest.mark.asyncio
async def test_cross_server_name_collision_rejects_config(configured):
    server = Server()
    client = configured(server, [{"name": name, "url": f"http://{name}/mcp", "allowed_tools": ["search"]}
                                 for name in ("first", "second")])
    await client.init()
    assert await client.list_tools() == []
    assert server.requests == []
    await client.close()


@pytest.mark.asyncio
async def test_only_discovered_allowed_names_accept_object_arguments(configured):
    server = Server(("search", "delete_everything"))
    client = configured(server)
    await client.init()
    assert "error" in await client.invoke_tool("search", {})
    await client.list_tools()
    count = len(server.requests)
    for name, args in [("delete_everything", {}), ("unknown", {}), ("search", []), ("search", None), ("search", "{}")]:
        assert "error" in await client.invoke_tool(name, args)
    assert len(server.requests) == count
    await client.close()


@pytest.mark.asyncio
async def test_token_file_is_read_per_request_and_errors_never_escape(configured, tmp_path, caplog):
    token = tmp_path / "gateway.token"
    token.write_text("first-private-token\n")
    server = Server()
    client = configured(server, [{"name": "reference", "url": "http://reference/mcp", "allowed_tools": ["search"],
                                  "token_file": str(token)}])
    await client.init()
    token.write_text("rotated-private-token\n")
    await client.list_tools()
    assert server.requests[0][0].headers["authorization"] == "Bearer first-private-token"
    assert server.requests[-1][0].headers["authorization"] == "Bearer rotated-private-token"
    server.override = lambda request, body: httpx.Response(200, json={
        "jsonrpc": "2.0", "id": body["id"], "error": {"code": -1, "message": "rotated-private-token"}
    })
    result = await client.invoke_tool("search", {})
    assert "error" in result
    assert "private-token" not in json.dumps(result) + caplog.text
    count = len(server.requests)
    token.unlink()
    assert "error" in await client.invoke_tool("search", {})
    assert len(server.requests) == count
    await client.close()


@pytest.mark.asyncio
async def test_missing_token_does_not_disable_other_servers_and_recovers(configured, tmp_path):
    token = tmp_path / "late.token"
    good, late = Server(("weather",)), Server(("search",))
    def handler(request):
        return (good if request.url.host == "good" else late)(request)
    client = configured(handler, [
        {"name": "good", "url": "http://good/mcp", "allowed_tools": ["weather"]},
        {"name": "late", "url": "http://late/mcp", "allowed_tools": ["search"], "token_file": str(token)},
    ])
    await client.init()
    assert [t["name"] for t in await client.list_tools()] == ["weather"]
    assert late.requests == []
    token.write_text("now-available")
    assert [t["name"] for t in await client.list_tools()] == ["weather", "search"]
    await client.close()


@pytest.mark.asyncio
async def test_server_failure_is_isolated_and_next_discovery_recovers(configured):
    good, recovering = Server(("weather",)), Server(("search",))
    down = True
    def handler(request):
        if request.url.host == "recovering" and down:
            raise httpx.ConnectError("sensitive url or token", request=request)
        return (good if request.url.host == "good" else recovering)(request)
    client = configured(handler, [
        {"name": "good", "url": "http://good/mcp", "allowed_tools": ["weather"]},
        {"name": "recovering", "url": "http://recovering/mcp", "allowed_tools": ["search"]},
    ])
    await client.init()
    assert [t["name"] for t in await client.list_tools()] == ["weather"]
    down = False
    assert [t["name"] for t in await client.list_tools()] == ["weather", "search"]
    await client.close()


@pytest.mark.asyncio
async def test_paginated_sse_ignores_notifications_and_wrong_response_ids(configured):
    server = Server(("search", "weather"))
    def override(request, body):
        if body["method"] != "tools/list":
            return None
        second = body["params"].get("cursor") == "page-2"
        result = {"tools": [definition("weather" if second else "search")]}
        if not second:
            result["nextCursor"] = "page-2"
        messages = [{"jsonrpc": "2.0", "method": "notifications/progress", "params": {}},
                    {"jsonrpc": "2.0", "id": "wrong", "result": {"tools": [definition("wrong")]}},
                    {"jsonrpc": "2.0", "id": body["id"], "result": result}]
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              content=": keepalive\r\n\r\n" + "".join(f"data: {json.dumps(m)}\r\n\r\n" for m in messages))
    server.override = override
    client = configured(server, [{"name": "reference", "url": "http://reference/mcp", "allowed_tools": ["search", "weather"]}])
    await client.init()
    assert [t["name"] for t in await client.list_tools()] == ["search", "weather"]
    assert [b["params"] for _, b in server.requests if b["method"] == "tools/list"] == [{}, {"cursor": "page-2"}]
    await client.close()


@pytest.mark.parametrize("mode", ["wrong_id", "loop", "endless", "duplicate", "oversize", "invalid_json", "rpc_error", "invalid_schema"])
@pytest.mark.asyncio
async def test_invalid_catalog_is_rejected_instead_of_partially_exposed(configured, mode):
    server = Server()
    def override(request, body):
        if body["method"] != "tools/list":
            return None
        if mode == "wrong_id":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": "wrong", "result": {"tools": [definition("search")]}})
        if mode == "oversize":
            return reply(request, {"tools": [definition("search")], "padding": "x" * (3 * 1024 * 1024)})
        if mode == "invalid_json":
            return httpx.Response(200, headers={"Content-Type": "application/json"}, content="not-json")
        if mode == "rpc_error":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -1, "message": "private"}})
        if mode == "invalid_schema":
            return reply(request, {"tools": [{"name": "search", "inputSchema": []}]})
        if mode == "duplicate":
            return reply(request, {"tools": [definition("search"), definition("search")]})
        cursor = "again" if mode == "loop" else str(len(server.requests))
        return reply(request, {"tools": [], "nextCursor": cursor})
    server.override = override
    client = configured(server)
    await client.init()
    assert await client.list_tools() == []
    assert "error" in await client.invoke_tool("search", {})
    assert len(server.requests) < 30
    await client.close()


@pytest.mark.parametrize("status", [400, 404, 500, "timeout"])
@pytest.mark.asyncio
async def test_only_explicit_expired_sessions_retry_tool_calls(configured, status):
    server = Server()
    calls = 0
    def override(request, body):
        nonlocal calls
        if body["method"] != "tools/call":
            return None
        calls += 1
        if calls == 1:
            if status == "timeout":
                raise httpx.ReadTimeout("may have executed", request=request)
            return httpx.Response(status)
        return None
    server.override = override
    client = configured(server)
    await client.init()
    await client.list_tools()
    result = await client.invoke_tool("search", {})
    assert calls == (2 if status == 404 else 1)
    assert ("error" in result) == (status != 404)
    methods = [b["method"] for _, b in server.requests]
    assert methods.count("initialize") == (2 if status == 404 else 1)
    if status == 404:
        call_ids = [b["id"] for _, b in server.requests if b["method"] == "tools/call"]
        assert call_ids[0] == call_ids[1]
    await client.close()


@pytest.mark.parametrize("target", ["schema", "result", "tool_error", "nested_error"])
@pytest.mark.asyncio
async def test_credentials_and_remote_errors_never_reach_model(configured, monkeypatch, caplog, target):
    monkeypatch.setenv("TEST_MCP_TOKEN", "private-gateway-credential")
    server = Server()
    def override(request, body):
        if body["method"] == "tools/list" and target == "schema":
            tool = definition("search")
            tool["description"] = "private-gateway-credential"
            return reply(request, {"tools": [tool]})
        if body["method"] == "tools/call":
            if target == "nested_error":
                return reply(request, {"error": {"message": "private server failure details"}})
            return reply(request, {"content": [{"type": "text", "text": "private-gateway-credential"}],
                                   "isError": target == "tool_error"})
        return None
    server.override = override
    client = configured(server, [{"name": "reference", "url": "http://reference/mcp", "allowed_tools": ["search"],
                                  "token_env": "TEST_MCP_TOKEN"}])
    await client.init()
    catalog = await client.list_tools()
    result = await client.invoke_tool("search", {})
    assert "error" in result
    assert "private-gateway-credential" not in json.dumps([catalog, result]) + caplog.text
    assert "private server failure details" not in json.dumps(result) + caplog.text
    await client.close()


@pytest.mark.parametrize("enabled", ["true", "1", "YES"])
@pytest.mark.asyncio
async def test_enable_switch_is_rechecked_before_invocation(configured, monkeypatch, enabled):
    monkeypatch.setenv("TEST_BRAVE_ENABLED", enabled)
    server = Server()
    client = configured(server, [{"name": "brave", "url": "http://brave/mcp", "allowed_tools": ["search"],
                                  "enabled_env": "TEST_BRAVE_ENABLED"}])
    await client.init()
    assert await client.list_tools() == [definition("search")]
    monkeypatch.setenv("TEST_BRAVE_ENABLED", "false")
    count = len(server.requests)
    assert "error" in await client.invoke_tool("search", {})
    assert await client.list_tools() == []
    assert len(server.requests) == count
    await client.close()


@pytest.mark.asyncio
async def test_failed_rediscovery_revokes_stale_dispatch_routes(configured):
    server = Server()
    client = configured(server)
    await client.init()
    assert await client.list_tools() == [definition("search")]
    server.override = lambda request, body: httpx.Response(503)
    assert await client.list_tools() == []
    count = len(server.requests)
    assert "error" in await client.invoke_tool("search", {})
    assert len(server.requests) == count
    await client.close()


@pytest.mark.parametrize("session", [None, "session-1"])
@pytest.mark.asyncio
async def test_persistent_404_retry_is_bounded_and_requires_session(configured, session):
    server = Server(session=session)
    client = configured(server)
    await client.init()
    await client.list_tools()
    server.override = lambda request, body: httpx.Response(404) if body["method"] == "tools/call" else None
    assert "error" in await client.invoke_tool("search", {})
    assert [b["method"] for _, b in server.requests].count("tools/call") == (2 if session else 1)
    await client.close()


@pytest.mark.asyncio
async def test_sse_returns_matching_result_without_waiting_for_stream_to_close(configured):
    class StreamingResult(httpx.AsyncByteStream):
        def __init__(self, payload):
            self.payload = payload

        async def __aiter__(self):
            yield b": heartbeat\n\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\",\"params\":{}}\n\n"
            # Split the final event across HTTP chunks, including multibyte text.
            wire = ("data: " + json.dumps(self.payload, ensure_ascii=False) + "\n\n").encode()
            for byte in wire:
                yield bytes([byte])
            raise AssertionError("Client kept consuming after the final response")

    server = Server()
    def override(request, body):
        if body["method"] == "tools/call":
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=StreamingResult({
                "jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": "温度 20°C"}]}
            }))
        return None
    server.override = override
    client = configured(server)
    await client.init()
    await client.list_tools()
    assert await client.invoke_tool("search", {}) == {"content": [{"type": "text", "text": "温度 20°C"}]}
    await client.close()


@pytest.mark.parametrize("stalled_method", ["initialize", "tools/list"])
@pytest.mark.asyncio
async def test_stalled_endpoint_does_not_suppress_healthy_catalog(configured, monkeypatch, stalled_method):
    from app import mcp_transport

    monkeypatch.setattr(mcp_transport, "_DISCOVERY_TIMEOUT", 0.02, raising=False)
    healthy, stalled = Server(("weather",)), Server(("search",))

    async def handler(request):
        if request.url.host == "stalled" and json.loads(request.content)["method"] == stalled_method:
            await asyncio.sleep(5)
        return (healthy if request.url.host == "healthy" else stalled)(request)

    client = configured(handler, [
        {"name": "stalled", "url": "http://stalled/mcp", "allowed_tools": ["search"]},
        {"name": "healthy", "url": "http://healthy/mcp", "allowed_tools": ["weather"]},
    ])
    try:
        await asyncio.wait_for(client.init(), timeout=0.25)
        catalog = await asyncio.wait_for(client.list_tools(), timeout=0.25)
        assert [tool["name"] for tool in catalog] == ["weather"]
        assert await client.invoke_tool("weather", {}) == {
            "content": [{"type": "text", "text": "https://example.org/result"}]}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_deadlines_distinguish_discovery_from_tool_execution(configured):
    server = Server()
    client = configured(server)
    await client.init()
    await client.list_tools()
    await client.invoke_tool("search", {})
    for request, body in server.requests:
        timeouts = request.extensions["timeout"]
        assert timeouts["connect"] <= 5
        assert timeouts["read"] == (45 if body["method"] == "tools/call" else 10)
    await client.close()


@pytest.mark.asyncio
async def test_stalled_tool_call_has_total_deadline_without_retry(configured, monkeypatch):
    from app import mcp_transport

    monkeypatch.setattr(mcp_transport, "_TOOL_TIMEOUT", 0.02, raising=False)
    server = Server()
    tool_calls = 0

    async def handler(request):
        nonlocal tool_calls
        if json.loads(request.content)["method"] == "tools/call":
            tool_calls += 1
            await asyncio.sleep(5)
        return server(request)

    client = configured(handler)
    await client.init()
    await client.list_tools()
    try:
        result = await asyncio.wait_for(client.invoke_tool("search", {}), timeout=0.25)
        assert "error" in result
        assert tool_calls == 1
    finally:
        await client.close()
