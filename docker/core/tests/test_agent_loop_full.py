"""Tests for app.agent_loop dataclasses + _extract_tool_text + AgentLoop builtins."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent_loop import AgentLoop, AgentResult, AgentStep, _extract_tool_text


class TestAgentStep:
    def test_minimal_construction(self):
        s = AgentStep(type="thinking", content="reasoning...")
        assert s.type == "thinking"
        assert s.content == "reasoning..."
        assert s.tool_name is None
        assert s.tool_args is None
        assert s.tool_result is None

    def test_tool_call_step(self):
        s = AgentStep(
            type="tool_call",
            content="invoking search",
            tool_name="web_search",
            tool_args={"q": "test"},
        )
        assert s.tool_name == "web_search"
        assert s.tool_args == {"q": "test"}


class TestAgentResult:
    def test_default_construction(self):
        r = AgentResult(response="hello")
        assert r.response == "hello"
        assert r.steps == []
        assert r.tools_used == []
        assert r.iterations == 0
        assert r.model  # has the default

    def test_steps_isolated_per_instance(self):
        r1 = AgentResult(response="a")
        r2 = AgentResult(response="b")
        r1.steps.append(AgentStep(type="thinking", content="x"))
        assert r2.steps == []  # not shared

    def test_tools_used_isolated_per_instance(self):
        r1 = AgentResult(response="a")
        r2 = AgentResult(response="b")
        r1.tools_used.append("search")
        assert r2.tools_used == []


class TestExtractToolText:
    def test_empty_content(self):
        result = _extract_tool_text({"content": []})
        assert isinstance(result, str)

    def test_text_item_list(self):
        result = _extract_tool_text({"content": [{"type": "text", "text": "hello"}]})
        assert result == "hello"

    def test_multiple_text_items_joined(self):
        result = _extract_tool_text({"content": [
            {"type": "text", "text": "line 1"},
            {"type": "text", "text": "line 2"},
        ]})
        assert "line 1" in result
        assert "line 2" in result

    def test_html_stripped(self):
        result = _extract_tool_text({"content": [
            {"type": "text", "text": "Hello <b>world</b> from <em>klukai</em>"},
        ]})
        assert "<b>" not in result
        assert "</em>" not in result
        assert "Hello" in result
        assert "world" in result

    def test_whitespace_collapsed(self):
        result = _extract_tool_text({"content": [
            {"type": "text", "text": "many   spaces\n\nand\t\ttabs"},
        ]})
        # All consecutive whitespace becomes a single space
        assert "  " not in result

    def test_plain_string_content(self):
        result = _extract_tool_text({"content": "plain string content"})
        assert result == "plain string content"

    def test_error_field(self):
        result = _extract_tool_text({"error": "tool failed"})
        assert "Error" in result
        assert "tool failed" in result

    def test_dict_without_type_but_with_text(self):
        result = _extract_tool_text({"content": [{"text": "no type field"}]})
        assert "no type field" in result

    def test_mixed_string_and_dict_items(self):
        result = _extract_tool_text({"content": [
            {"type": "text", "text": "first"},
            "raw string",
        ]})
        assert "first" in result
        assert "raw string" in result


class TestAgentLoopBuiltinRecallMemory:
    @pytest.mark.asyncio
    async def test_recalls_facts_and_episodes(self):
        router = MagicMock()
        mcp = MagicMock()
        ws = MagicMock()
        loop = AgentLoop(router=router, mcp=mcp, ws=ws)

        with patch("app.context.memory") as mem:
            mem.recall_facts_by_pattern = AsyncMock(return_value=[
                {"value": "Commander's birthday is March 5"},
            ])
            mem.recall_episodes = AsyncMock(return_value=[
                {"summary": "Briefing went well"},
            ])
            result = await loop._builtin_recall_memory({"query": "birthday"}, "alice")

        # Result is a dict with content list — _extract_tool_text style
        assert isinstance(result, dict)
        # Content should reference the facts
        text = str(result).lower()
        assert "march 5" in text or "birthday" in text

    @pytest.mark.asyncio
    async def test_empty_query_handled(self):
        router = MagicMock()
        mcp = MagicMock()
        ws = MagicMock()
        loop = AgentLoop(router=router, mcp=mcp, ws=ws)

        with patch("app.context.memory") as mem:
            mem.recall_facts_by_pattern = AsyncMock(return_value=[])
            mem.recall_episodes = AsyncMock(return_value=[])
            result = await loop._builtin_recall_memory({"query": ""}, "alice")
        # Returns a dict regardless
        assert isinstance(result, dict)
