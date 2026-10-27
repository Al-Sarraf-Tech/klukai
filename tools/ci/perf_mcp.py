"""The perf cores' MCP configuration: one server, the isolated fixture.

Since 2026-10-27 core has no unconfigured MCP gateway (the aichat-mcp
fallback is gone), so both measured cores read this file
(KLUKAI_MCP_CONFIG) and open their MCP session at startup against the
fixture's /mcp. perf_evidence counts that handshake as proof each core ran
wired to the fixture. The pre-2026-10-27 baseline honours the same setting.
"""

import json

MCP_CONFIG_NAME = "mcp-tools.json"
MCP_CONFIG = json.dumps(
    {
        "servers": [
            {
                "name": "fixture",
                "url": "http://fixture:8088/mcp",
                "allowed_tools": ["fixture_probe"],
            }
        ]
    },
    indent=2,
) + "\n"
