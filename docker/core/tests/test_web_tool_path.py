"""Web routing and the real schema/agent path, with external I/O replaced."""

from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import tool_schemas
from app.agent_loop import AgentLoop, _extract_tool_text
from app.llm_router import LLMRouter
from app.models import LLMConfig


SEARCH = {
    "name": "brave_web_search",
    "description": "Search public web pages",
    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                    "required": ["query"]},
}


class Catalog:
    def __init__(self, catalogs=None):
        self.catalogs = catalogs or [[deepcopy(SEARCH)]]
        self.discoveries = 0
        self.recoveries = 0
        self.calls = []

    async def list_tools(self):
        value = self.catalogs[min(self.discoveries, len(self.catalogs) - 1)]
        self.discoveries += 1
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)

    async def _initialize_session(self):
        self.recoveries += 1

    async def invoke_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text":
            '<a href="https://weather.example/chicago">Chicago forecast</a>: 15 C.'}]}


@pytest.fixture(autouse=True)
def reset_catalog_cache():
    tool_schemas.clear_cache()
    yield
    tool_schemas.clear_cache()


@pytest.mark.parametrize("query", [
    "Can you search the web for Chicago weather?",
    "Could you check online for the latest news?",
    "What is the weather like in Chicago?",
    "Klukai, look up the current price of coffee for me.",
    "Can you find out what the stock market did today?",
    "Read https://example.com and summarize it",
    "Could you convert 12 miles to kilometers?",
    "How many pounds is 5 kg?",
    "What is 50 USD in EUR?",
    "Could you check the exchange rate for yen?",
    "Find an arXiv paper about transformers for me",
    "Look up DOI 10.1000/example",
    "Can you find Python documentation for asyncio?",
    "Find a Stack Overflow reference for this error",
    "Could you look up Ada Lovelace in the offline encyclopedia?",
    "Can you search for references about the Roman Empire?",
    "What are the latest headlines?",
    "Can you tell me today's forecast?",
    "Will it rain in Chicago tomorrow?",
    "What is the current temperature in Chicago?",
    "Can you check the latest version of Python?",
    "Could you find who is the president of France?",
    "Can you look up who leads the team at OpenAI?",
    "Could you find the latest news about the mission to Europa?",
    "Can you look up the team at NASA?",
    "Find news about the mission to Mars",
])
async def test_natural_external_requests_have_tools(query):
    router = LLMRouter()
    router._anthropic_http = AsyncMock()
    assert await router.needs_agent(query) is True


@pytest.mark.parametrize("query", [
    "What's the weather like in your sector?",
    "Tell me the latest news from our squad.",
    "Who is the squad leader?",
    "Search our base for the missing soldier.",
    "How do you feel today?",
    "I like your new look.",
    "Do you remember our time together?",
    "The weather is lovely today.",
    "Our mission is right now.",
    "What do you think of the team?",
    "Did you search your memories?",
    "Do you like rainy weather?",
    "Tell me about your latest dream",
    "What is your current mood?",
    "How is your current mission going?",
    "Tell me the latest from your squad",
])
async def test_roleplay_and_casual_chat_do_not_trigger_tools(query):
    router = LLMRouter()
    router._anthropic_http = AsyncMock()
    assert await router.needs_agent(query) is False


async def test_schema_cache_isolated_between_clients():
    brave = Catalog()
    encyclopedia = Catalog([[{"name": "encyclopedia_lookup"}]])
    assert (await tool_schemas.get_tool_schemas(brave))[0]["function"]["name"] == "brave_web_search"
    assert (await tool_schemas.get_tool_schemas(encyclopedia))[0]["function"]["name"] == "encyclopedia_lookup"


async def test_empty_catalog_recovers_without_waiting_for_expiry():
    catalog = Catalog([[], [SEARCH]])
    assert await tool_schemas.get_tool_schemas(catalog) == []
    assert (await tool_schemas.get_tool_schemas(catalog))[0]["function"]["name"] == "brave_web_search"


async def test_catalog_refreshes_after_one_minute(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    catalog = Catalog([[SEARCH], [{"name": "free_web_search"}]])
    first = await tool_schemas.get_tool_schemas(catalog)
    now[0] = 159.0
    assert await tool_schemas.get_tool_schemas(catalog) == first
    now[0] = 161.0
    assert (await tool_schemas.get_tool_schemas(catalog))[0]["function"]["name"] == "free_web_search"


async def test_discovery_failure_cannot_leak_other_clients_catalog():
    await tool_schemas.get_tool_schemas(Catalog())
    assert await tool_schemas.get_tool_schemas(Catalog([RuntimeError("unavailable")])) == []


def tool_request(name, arguments):
    return {"choices": [{"message": {"tool_calls": [{"id": "search-1", "type": "function",
        "function": {"name": name, "arguments": arguments}}]}}]}


async def test_cold_catalog_recovers_and_search_sources_reach_synthesis():
    catalog = Catalog([[], [SEARCH]])
    requests = []

    async def complete(system, messages, config, tools=None):
        requests.append(deepcopy(messages))
        if len(requests) == 1:
            assert "brave_web_search" in {tool["function"]["name"] for tool in tools}
            return tool_request("brave_web_search", '{"query": "Chicago weather"}')
        tool_message = messages[-1]
        assert tool_message["tool_call_id"] == "search-1"
        assert "15 C" in tool_message["content"]
        assert "https://weather.example/chicago" in tool_message["content"]
        return {"choices": [{"message": {"content":
            "It is 15 C. [Forecast](https://weather.example/chicago)"}}]}

    router = MagicMock()
    router.route = AsyncMock(return_value=LLMConfig(
        provider="anthropic", model="claude-haiku-5-5",
        base_url="https://api.anthropic.com/v1/messages"))
    router.complete_chat = complete
    result = await AgentLoop(router, catalog, AsyncMock()).run(
        "Use retrieved evidence and cite its sources.",
        [{"role": "user", "content": "Can you search for Chicago weather?"}],
    )
    assert catalog.recoveries == 1
    assert catalog.calls == [("brave_web_search", {"query": "Chicago weather"})]
    assert result.response == "It is 15 C. [Forecast](https://weather.example/chicago)"
    assert result.tools_used == ["brave_web_search"]


@pytest.mark.parametrize("name, arguments", [
    ("shell", '{"command": "uname"}'),
    ("brave_web_search", "[]"),
    ("brave_web_search", "null"),
    ("brave_web_search", "invalid-json"),
    ("brave_web_search", 42),
    ("recall_memory", "[]"),
    (["shell"], "{}"),
    (None, "{}"),
])
async def test_local_tool_selection_rejects_undeclared_names_and_nonobjects(name, arguments):
    catalog = Catalog()
    requests = []

    async def complete(system, messages, config, tools=None):
        requests.append(deepcopy(messages))
        if len(requests) == 1:
            return tool_request(name, arguments)
        assert messages[-1]["role"] == "tool"
        assert "Error:" in messages[-1]["content"]
        return {"choices": [{"message": {"content": "That tool request was rejected."}}]}

    router = MagicMock()
    router.route = AsyncMock(return_value=LLMConfig(
        provider="anthropic", model="claude-haiku-5-5",
        base_url="https://api.anthropic.com/v1/messages"))
    router.complete_chat = complete
    result = await AgentLoop(router, catalog, AsyncMock()).run(
        "persona", [{"role": "user", "content": "look up a reference"}],
    )
    assert catalog.calls == []
    assert result.tools_used == []
    assert result.response == "That tool request was rejected."


def test_extract_preserves_html_links_and_markdown_autolinks():
    output = _extract_tool_text({"content": [{"type": "text", "text":
        '<p>Source: <a href="https://example.org/a?x=1&amp;y=2">Study</a></p>'
        '\n<https://example.org/other>'}]})
    assert "https://example.org/a?x=1&y=2" in output
    assert "https://example.org/other" in output
    assert "Study" in output


async def test_long_result_keeps_sources_inside_bounded_model_context():
    catalog = Catalog()
    catalog.invoke_tool = AsyncMock(return_value={"content": [{"type": "text", "text":
        "Finding. " * 3000 + "\nSource: https://example.org/evidence"}]})
    requests = []

    async def complete(system, messages, config, tools=None):
        requests.append(deepcopy(messages))
        if len(requests) == 1:
            return tool_request("brave_web_search", '{"query":"public evidence"}')
        assert len(messages[-1]["content"]) <= 8000
        assert "https://example.org/evidence" in messages[-1]["content"]
        assert "truncated" in messages[-1]["content"].lower()
        return {"choices": [{"message": {"content": "Found evidence."}}]}

    router = MagicMock()
    router.route = AsyncMock(return_value=LLMConfig(
        provider="anthropic", model="claude-haiku-5-5",
        base_url="https://api.anthropic.com/v1/messages"))
    router.complete_chat = complete
    result = await AgentLoop(router, catalog, AsyncMock()).run(
        "persona", [{"role": "user", "content": "find a reference"}],
    )
    assert result.response == "Found evidence."
