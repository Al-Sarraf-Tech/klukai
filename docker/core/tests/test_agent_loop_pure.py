"""Pure tests for agent_loop — dataclasses + _extract_tool_text."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestAgentStepDataclass:
    def test_defaults(self):
        from app.agent_loop import AgentStep
        s = AgentStep(type="thinking", content="hello")
        assert s.type == "thinking"
        assert s.tool_name is None
        assert s.tool_args is None

    def test_full_init(self):
        from app.agent_loop import AgentStep
        s = AgentStep(type="tool_call", content="calling",
                       tool_name="web_search",
                       tool_args={"q": "klukai"},
                       tool_result={"ok": True})
        assert s.tool_name == "web_search"
        assert s.tool_args == {"q": "klukai"}


class TestAgentResultDataclass:
    def test_defaults(self):
        from app.agent_loop import AgentResult, LOCAL_AGENT
        r = AgentResult(response="hi")
        assert r.response == "hi"
        assert r.steps == []
        assert r.tools_used == []
        assert r.iterations == 0
        assert r.model == LOCAL_AGENT

    def test_empty_lists_are_independent(self):
        from app.agent_loop import AgentResult
        a = AgentResult(response="a")
        b = AgentResult(response="b")
        a.steps.append("x")
        assert len(b.steps) == 0  # default_factory gives fresh list


class TestToolStatusMessages:
    def test_has_web_search(self):
        from app.agent_loop import TOOL_STATUS
        assert "web_search" in TOOL_STATUS
        assert isinstance(TOOL_STATUS["web_search"], str)

    def test_all_values_non_empty(self):
        from app.agent_loop import TOOL_STATUS
        for tool, msg in TOOL_STATUS.items():
            assert msg and isinstance(msg, str)


class TestExtractToolText:
    def test_empty_content(self):
        from app.agent_loop import _extract_tool_text
        assert _extract_tool_text({}) == "{}" or isinstance(_extract_tool_text({}), str)

    def test_text_content_list(self):
        from app.agent_loop import _extract_tool_text
        result = {"content": [{"type": "text", "text": "Hello"}]}
        assert _extract_tool_text(result) == "Hello"

    def test_multiple_text_items_joined(self):
        from app.agent_loop import _extract_tool_text
        result = {"content": [
            {"type": "text", "text": "First"},
            {"type": "text", "text": "Second"},
        ]}
        out = _extract_tool_text(result)
        assert "First" in out and "Second" in out

    def test_strips_html_tags(self):
        from app.agent_loop import _extract_tool_text
        result = {"content": [{"type": "text", "text": "<p>Hello <b>there</b></p>"}]}
        out = _extract_tool_text(result)
        assert "<" not in out
        assert "Hello" in out
        assert "there" in out

    def test_collapses_whitespace(self):
        from app.agent_loop import _extract_tool_text
        result = {"content": [{"type": "text", "text": "a    b\n\n\nc"}]}
        out = _extract_tool_text(result)
        assert "a b c" in out

    def test_string_content_returned_as_is(self):
        from app.agent_loop import _extract_tool_text
        assert _extract_tool_text({"content": "direct string"}) == "direct string"

    def test_string_content_tolerates_dict_without_type(self):
        """Item dict without 'type' key but with 'text' should still extract."""
        from app.agent_loop import _extract_tool_text
        result = {"content": [{"text": "naked text"}]}
        out = _extract_tool_text(result)
        assert "naked text" in out

    def test_string_item_in_list(self):
        from app.agent_loop import _extract_tool_text
        result = {"content": ["just a string"]}
        out = _extract_tool_text(result)
        assert "just a string" in out

    def test_error_field_surfaces(self):
        from app.agent_loop import _extract_tool_text
        result = {"error": "timeout"}
        out = _extract_tool_text(result)
        assert "timeout" in out

    def test_fallback_to_repr(self):
        from app.agent_loop import _extract_tool_text
        # Unknown shape — should still return *some* string
        out = _extract_tool_text({"weird": [1, 2, 3]})
        assert isinstance(out, str)


class TestBuiltinRecallMemory:
    @pytest.mark.asyncio
    async def test_returns_mcp_shape(self):
        """_builtin_recall_memory should always return MCP-style {content: [{type:text,...}]}."""
        from app.agent_loop import AgentLoop
        from unittest.mock import MagicMock

        loop = AgentLoop(router=MagicMock(), mcp=MagicMock(), ws=MagicMock())

        with patch("app.context.memory") as fake_memory:
            fake_memory.recall_facts_by_pattern = AsyncMock(return_value=[])
            fake_memory.recall_episodes = AsyncMock(return_value=[])

            result = await loop._builtin_recall_memory({"query": "test"}, user_id="alice")

        assert "content" in result
        assert isinstance(result["content"], list)
        assert result["content"][0]["type"] == "text"

    @pytest.mark.asyncio
    async def test_db_error_returns_error_content(self):
        from app.agent_loop import AgentLoop
        from unittest.mock import MagicMock

        loop = AgentLoop(router=MagicMock(), mcp=MagicMock(), ws=MagicMock())

        with patch("app.context.memory") as fake_memory:
            fake_memory.recall_facts_by_pattern = AsyncMock(
                side_effect=RuntimeError("db down"))

            result = await loop._builtin_recall_memory({"query": "test"}, user_id="alice")

        assert "content" in result
        assert "error" in result["content"][0]["text"].lower()

    @pytest.mark.asyncio
    async def test_empty_query_tolerated(self):
        from app.agent_loop import AgentLoop
        from unittest.mock import MagicMock

        loop = AgentLoop(router=MagicMock(), mcp=MagicMock(), ws=MagicMock())

        with patch("app.context.memory") as fake_memory:
            fake_memory.recall_facts_by_pattern = AsyncMock(return_value=[])
            fake_memory.recall_episodes = AsyncMock(return_value=[])

            result = await loop._builtin_recall_memory({}, user_id="alice")

        assert "content" in result
