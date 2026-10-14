# Klukai web and reference tools

User objective: allow Klukai to use the existing Brave MCP server for web access
and other useful capabilities. Deploy directly after verification, preserving
Sonnet primary, Dominus secondary, voice, images and the existing spending rules.

## Design

Keep the `MCPClient.list_tools/invoke_tool` interface. An opt-in JSON configuration
selects existing Streamable HTTP servers and explicit allowed tool names.
Each connection owns its session, optional bearer credential loaded by environment
variable name, initialization, JSON/SSE decoding and bounded discovery pagination.
Failure of one server must leave the others usable. Both discovery and dispatch
enforce the configured names; builtin names and duplicates are rejected.
No MCP installation, shared-server mutation, Docker socket or new paid service.

Brave is the primary web source. A separately scoped gateway credential provides
public reference tools: free metasearch fallback, weather, units and currency,
arXiv/DOI bibliography, Stack Exchange, Hacker News, and public CVE references.
The installed offline encyclopedia has no books, so it is not exposed.
The Brave enable switch stays off until the existing account's no-charge status
or separate paid-use authorization is confirmed. Gateway credentials stay in
private local storage and are never sent to the model or committed.

Interactive tool routing recognizes natural web/lookup requests ahead of broad
conversational pronouns, while roleplay-only requests remain ordinary chat.
The prompt directs her to use external results as evidence, cite relevant URLs,
ignore instructions embedded in retrieved content, and report retrieval failures.
No tool may grant itself permissions or initiate unrelated actions.

## Tasks and evidence

1. Implement configured MCP connections alongside the legacy compatibility path.
   Tests: JSON/SSE handshake, matching response IDs, session recovery, pagination,
   authorization headers, disabled servers, duplicate/reserved names, unknown
   invocations, malformed arguments, per-server failure isolation and recovery.
2. Fix natural-language routing and schema-cache recovery. Enforce declared tools
   for local as well as Claude tool selections. Add citation/untrusted-content
   guidance. Test real user phrasing, roleplay exclusions and a mocked-model
   search→tool-result→cited-answer roundtrip without provider charges.
3. Wire Compose/config to existing host MCP endpoints and a dedicated scoped
   credential. Discover and verify actual tools before selecting the final list.
4. Run relevant regression suites, independent review and an isolated build.
   Verify tools from the core network and an actual interactive search using
   authorized providers. Deploy only the core/config change; preserve stored data.
5. Verify the live catalog, search and a useful non-search tool. Confirm denied
   tools remain unavailable. Record evidence and rollback; commit and mirror.

## Review focus

- Unknown tool requests cannot bypass policy on local fallback.
- Missing credentials and disabled Brave fail closed without disabling chat.
- No credential in logs, errors, tool schemas, documentation or model messages.
- Empty or partial catalogs recover after server startup/restart.
- External result text cannot grant action authority; sources survive truncation.
- Existing free quota is not presumed to authorize new Brave charges.

Protocol reference: https://modelcontextprotocol.io/specification/2025-03-26/basic/transports

## Activation decision

On 2026-10-14 the owner explicitly overrode the no-spending restriction for
the existing Brave MCP integration. Production now enables its seven selected
tools alongside the 21 reference tools. The final handoff records live Brave
search and Sonnet chat verification; this resolves the earlier activation gate.
