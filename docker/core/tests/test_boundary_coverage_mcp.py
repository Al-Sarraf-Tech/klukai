"""Fail-closed MCP configuration, response framing, and session boundaries."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app import mcp_policy as policy
from app import mcp_transport as transport


def rpc(result, request_id="expected"):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


@pytest.mark.parametrize(
    "content",
    [
        "data: " + json.dumps(rpc({"ok": True})),
        "data: " + json.dumps(rpc({"ok": True})) + "\n",
        "data: "
        + json.dumps(rpc({"other": True}, "other"))
        + "\n\ndata: "
        + json.dumps(rpc({"ok": True})),
    ],
)
async def test_sse_last_event_without_blank_terminator_is_consumed(content):
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream; charset=utf-8"},
        content=content,
    )
    assert await transport._read_response(response, "expected") == {"ok": True}


@pytest.mark.parametrize(
    "content_type,content,reason",
    [
        ("text/html", b"<html>private peer body</html>", "Unsupported"),
        ("application/json", json.dumps(rpc([])).encode(), "Invalid MCP response"),
        (
            "application/json",
            json.dumps(rpc({"ignored": True}, "other")).encode(),
            "Missing",
        ),
        ("text/event-stream", b": keepalive\n", "Missing"),
        ("text/event-stream", b"event: ping", "Missing"),
        (
            "text/event-stream",
            ("data: " + json.dumps(rpc({}, "other"))).encode(),
            "Missing",
        ),
        (
            "text/event-stream",
            ("data: " + json.dumps(rpc({}, "other")) + "\n").encode(),
            "Missing",
        ),
    ],
)
async def test_incomplete_or_nonmatching_peer_response_never_becomes_a_result(
    content_type, content, reason
):
    response = httpx.Response(
        200, headers={"content-type": content_type}, content=content
    )
    with pytest.raises(transport.MCPFailure, match=reason) as failure:
        await transport._read_response(response, "expected")
    assert "private peer body" not in str(failure.value)


def test_anonymous_session_headers_carry_session_without_inventing_credentials():
    connection = transport.MCPConnection(
        policy.ServerConfig("fixture", "http://peer.invalid/mcp", frozenset({"safe"})),
        None,
    )
    connection._session_id = "session-fixture"
    assert connection._headers() == {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-03-26",
        "Mcp-Session-Id": "session-fixture",
    }


@pytest.mark.parametrize(
    "case,reason",
    [
        ("notification", "initialization not accepted"),
        ("bad-session", "Invalid MCP session"),
        ("bad-protocol", "Unsupported MCP protocol"),
    ],
)
async def test_bad_handshake_never_marks_connection_initialized(case, reason):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body["method"])
        if "id" not in body:
            return httpx.Response(200)
        result = {
            "protocolVersion": "wrong" if case == "bad-protocol" else "2025-03-26"
        }
        headers = (
            {"Mcp-Session-Id": ""}
            if case == "bad-session"
            else {"Mcp-Session-Id": "valid-session"}
        )
        return httpx.Response(200, headers=headers, json=rpc(result, body["id"]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connection = transport.MCPConnection(
            policy.ServerConfig(
                "fixture", "http://peer.invalid/mcp", frozenset({"safe"})
            ),
            client,
        )
        with pytest.raises(transport.MCPFailure, match=reason):
            await connection.initialize()
        assert connection._initialized is False and connection._session_id is None
    assert calls == (
        ["initialize", "notifications/initialized"]
        if case == "notification"
        else ["initialize"]
    )


@pytest.mark.parametrize(
    "catalog,reason",
    [
        ({"tools": None}, "Invalid MCP catalog"),
        ({"tools": ["not-an-object"]}, "Invalid MCP tool"),
        ({"tools": [{"name": 7}]}, "Invalid MCP tool"),
    ],
)
async def test_catalog_shape_is_validated_before_advertising_any_tool(
    monkeypatch, catalog, reason
):
    connection = transport.MCPConnection(
        policy.ServerConfig("fixture", "http://peer.invalid/mcp", frozenset({"safe"})),
        None,
    )
    request = AsyncMock(return_value=catalog)
    monkeypatch.setattr(connection, "request", request)
    with pytest.raises(transport.MCPFailure, match=reason):
        await connection.list_tools()
    request.assert_awaited_once_with("tools/list", {})


@pytest.mark.parametrize(
    "change",
    [
        "empty-tools",
        "invalid-tool",
        "reserved-tool",
        "duplicate-tool",
        "bad-enabled-env",
        "bad-token-env",
        "empty-token-file",
        "two-auth-methods",
        "unknown-server-field",
        "non-object-server",
        "non-text-url",
        "invalid-url",
        "too-many-servers",
        "oversized-config",
    ],
)
def test_complete_policy_rejects_invalid_entries_without_partial_admission(
    tmp_path, caplog, change
):
    server = {
        "name": "safe-peer",
        "url": "http://peer.invalid/mcp",
        "allowed_tools": ["safe_lookup"],
    }
    if change == "empty-tools":
        server["allowed_tools"] = []
    elif change == "invalid-tool":
        server["allowed_tools"] = ["has spaces"]
    elif change == "reserved-tool":
        server["allowed_tools"] = ["get_current_time"]
    elif change == "duplicate-tool":
        server["allowed_tools"] = ["safe_lookup", "safe_lookup"]
    elif change == "bad-enabled-env":
        server["enabled_env"] = "private bad env"
    elif change == "bad-token-env":
        server["token_env"] = 4
    elif change == "empty-token-file":
        server["token_file"] = ""
    elif change == "two-auth-methods":
        server.update(token_env="MCP_TOKEN", token_file="/private/token")
    elif change == "unknown-server-field":
        server["password"] = "never-log-me"
    elif change == "non-object-server":
        server = None
    elif change == "non-text-url":
        server["url"] = 17
    elif change == "invalid-url":
        server["url"] = "https://user:never-log-me@peer.invalid/mcp"
    servers = [server] * (17 if change == "too-many-servers" else 1)
    path = tmp_path / "mcp.json"
    path.write_text(
        " " * 65537
        if change == "oversized-config"
        else json.dumps({"servers": servers})
    )
    assert policy.load_config(str(path)) == []
    assert "configured tools disabled" in caplog.text
    assert "never-log-me" not in caplog.text and "/private/token" not in caplog.text


@pytest.mark.parametrize("token", ["", "two words", "x" * 8193, "\x7f"])
def test_unusable_bearer_never_becomes_an_outbound_header(monkeypatch, token):
    monkeypatch.setenv("BOUNDARY_MCP_TOKEN", token)
    config = policy.ServerConfig(
        "fixture",
        "http://peer.invalid",
        frozenset({"safe"}),
        token_env="BOUNDARY_MCP_TOKEN",
    )
    with pytest.raises(transport.MCPFailure, match="credential unavailable"):
        transport.MCPConnection(config, None)._headers()


def test_runtime_server_disable_prevents_header_construction(monkeypatch):
    monkeypatch.setenv("BOUNDARY_MCP_ENABLED", "false")
    config = policy.ServerConfig(
        "fixture",
        "http://peer.invalid",
        frozenset({"safe"}),
        enabled_env="BOUNDARY_MCP_ENABLED",
    )
    with pytest.raises(transport.MCPFailure, match="server disabled"):
        transport.MCPConnection(config, None)._headers()


def test_duplicate_server_identity_rejects_entire_configuration(tmp_path):
    config = {
        "servers": [
            {
                "name": "same",
                "url": "http://peer.invalid/a",
                "allowed_tools": ["first"],
            },
            {
                "name": "same",
                "url": "http://peer.invalid/b",
                "allowed_tools": ["second"],
            },
        ]
    }
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(config))
    assert policy.load_config(str(path)) == []
