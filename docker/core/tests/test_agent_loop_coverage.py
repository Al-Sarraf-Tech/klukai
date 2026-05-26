"""Behavioral coverage tests for app.agent_loop.AgentLoop.run().

Each test asserts a concrete behavior of the tool-use loop:
- which tool is dispatched (builtin vs MCP) and with what args,
- the assistant/tool message threading fed back to the model,
- multi-iteration continuation until a no-tool response terminates,
- MCP recovery when no tools load, per-tool timeout handling,
- the streaming fallback when the completion call raises,
- forced final synthesis when the loop ends with no response,
- the wall-clock timeout guard (clock patched — no real sleep).

Network/LLM/MCP are mocked at the object boundary. The monotonic clock is
patched where timing matters so behavior is deterministic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.agent_loop as al
from app.agent_loop import AgentLoop, AgentResult, AgentStep, _extract_tool_text


# ── Builders ────────────────────────────────────────────────────────────────


def _msg_response(content="", tool_calls=None) -> dict:
    """OpenAI-shaped chat completion with optional tool_calls."""
    message: dict = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}


def _tool_call(name, args='{}', tc_id="call_1") -> dict:
    return {"id": tc_id, "function": {"name": name, "arguments": args}}


def _mcp_text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _make_loop(complete_side_effect=None, list_tools=None):
    """Build an AgentLoop with mocked router/mcp/ws.

    Returns (loop, router, mcp, ws). get_tool_schemas/get_builtin_tools are NOT
    patched here — patch them per-test as needed.
    """
    router = MagicMock()
    router.complete_local = AsyncMock(side_effect=complete_side_effect)
    router.stream = MagicMock()
    mcp = MagicMock()
    mcp.list_tools = AsyncMock(return_value=list_tools or [])
    mcp.invoke_tool = AsyncMock()
    mcp._initialize_session = AsyncMock()
    ws = MagicMock()
    ws.send_thinking = AsyncMock()
    ws.send_tool_use = AsyncMock()
    loop = AgentLoop(router, mcp, ws)
    return loop, router, mcp, ws


@pytest.fixture(autouse=True)
def _clear_schema_cache():
    """tool_schemas caches globally — clear before each test for isolation."""
    import app.tool_schemas as ts

    ts.clear_cache()
    yield
    ts.clear_cache()


# ── Plain response, no tools (lines 164-172) ────────────────────────────────


class TestPlainResponse:
    @pytest.mark.asyncio
    async def test_no_tool_calls_returns_content_immediately(self):
        loop, router, mcp, ws = _make_loop(
            complete_side_effect=[_msg_response(content="Hello Commander.")]
        )
        with patch.object(al, "get_tool_schemas", AsyncMock(return_value=[{"x": 1}])), patch.object(
            al, "get_builtin_tools", return_value=[]
        ):
            res = await loop.run("sys", [{"role": "user", "content": "hi"}])
        assert res.response == "Hello Commander."
        assert res.iterations == 1
        assert res.tools_used == []
        # exactly one response step recorded
        assert [s.type for s in res.steps] == ["response"]
        # only one LLM round trip
        assert router.complete_local.await_count == 1
        ws.send_thinking.assert_awaited()  # "Analyzing request..."


# ── MCP recovery when no tools load (lines 105-113) ─────────────────────────


class TestMCPRecovery:
    @pytest.mark.asyncio
    async def test_empty_tools_triggers_session_recovery(self):
        loop, router, mcp, ws = _make_loop(
            complete_side_effect=[_msg_response(content="ok")]
        )
        # First get_tool_schemas returns [], then after recovery returns tools.
        gts = AsyncMock(side_effect=[[], [{"type": "function"}]])
        with patch.object(al, "get_tool_schemas", gts), patch.object(
            al, "get_builtin_tools", return_value=[]
        ):
            res = await loop.run("sys", [{"role": "user", "content": "hi"}])
        mcp._initialize_session.assert_awaited_once()
        assert gts.await_count == 2
        assert res.response == "ok"

    @pytest.mark.asyncio
    async def test_recovery_failure_is_swallowed(self):
        loop, router, mcp, ws = _make_loop(
            complete_side_effect=[_msg_response(content="ok")]
        )
        mcp._initialize_session = AsyncMock(side_effect=RuntimeError("mcp dead"))
        gts = AsyncMock(return_value=[])  # always empty
        with patch.object(al, "get_tool_schemas", gts), patch.object(
            al, "get_builtin_tools", return_value=[{"builtin": True}]
        ):
            res = await loop.run("sys", [{"role": "user", "content": "hi"}])
        # builtins still appended despite recovery failure
        passed_tools = router.complete_local.await_args.kwargs["tools"]
        assert {"builtin": True} in passed_tools
        assert res.response == "ok"


# ── Builtin tool dispatch: get_current_time (lines 207-208) ─────────────────


class TestBuiltinTimeDispatch:
    @pytest.mark.asyncio
    async def test_get_current_time_dispatched_then_synthesized(self):
        loop, router, mcp, ws = _make_loop(
            complete_side_effect=[
                _msg_response(tool_calls=[_tool_call("get_current_time")]),
                _msg_response(content="It is the morning, Commander."),
            ]
        )
        with patch.object(al, "get_tool_schemas", AsyncMock(return_value=[])), patch.object(
            al, "get_builtin_tools", return_value=[]
        ):
            res = await loop.run("sys", [{"role": "user", "content": "what time"}])
        # builtin path used — MCP NOT invoked
        mcp.invoke_tool.assert_not_awaited()
        assert "get_current_time" in res.tools_used
        assert res.response == "It is the morning, Commander."
        # tool_result step content is real UTC text from _builtin_get_time
        tr = [s for s in res.steps if s.type == "tool_result"][0]
        assert "UTC" in tr.content
        assert res.iterations == 2


# ── Builtin tool dispatch: recall_memory (lines 205-206, 64-87) ─────────────


class TestBuiltinRecallMemory:
    @pytest.mark.asyncio
    async def test_recall_memory_queries_memory_and_threads_result(self):
        loop, router, mcp, ws = _make_loop(
            complete_side_effect=[
                _msg_response(
                    tool_calls=[_tool_call("recall_memory", '{"query": "favorite tea"}')]
                ),
                _msg_response(content="You like green tea, Commander."),
            ]
        )
        fake_mem = MagicMock()
        fake_mem.recall_facts_by_pattern = AsyncMock(
            return_value=[{"value": "likes green tea"}]
        )
        fake_mem.recall_episodes = AsyncMock(
            return_value=[{"summary": "We discussed tea at the base."}]
        )
        ctx = MagicMock(memory=fake_mem)
        with patch.object(al, "get_tool_schemas", AsyncMock(return_value=[])), patch.object(
            al, "get_builtin_tools", return_value=[]
        ), patch.dict(sys.modules, {"app.context": ctx}):
            res = await loop.run(
                "sys", [{"role": "user", "content": "remember?"}], user_id="cmdr"
            )
        # pattern derived from query, user_id forwarded
        fake_mem.recall_facts_by_pattern.assert_awaited_once()
        pat = fake_mem.recall_facts_by_pattern.await_args.args[0]
        assert pat == "rel:%favorite%tea%"
        assert fake_mem.recall_facts_by_pattern.await_args.kwargs["user_id"] == "cmdr"
        fake_mem.recall_episodes.assert_awaited_once()
        tr = [s for s in res.steps if s.type == "tool_result"][0]
        assert "likes green tea" in tr.tool_result["content"][0]["text"]
        assert "discussed tea" in tr.tool_result["content"][0]["text"]
        assert res.response == "You like green tea, Commander."

    @pytest.mark.asyncio
    async def test_recall_memory_error_returns_error_text(self):
        loop = AgentLoop(MagicMock(), MagicMock(), MagicMock())
        ctx = MagicMock()
        ctx.memory.recall_facts_by_pattern = AsyncMock(
            side_effect=RuntimeError("qdrant down")
        )
        with patch.dict(sys.modules, {"app.context": ctx}):
            out = await loop._builtin_recall_memory({"query": "x"}, "u")
        assert "Memory search error" in out["content"][0]["text"]
        assert "qdrant down" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_recall_memory_no_results_text(self):
        loop = AgentLoop(MagicMock(), MagicMock(), MagicMock())
        ctx = MagicMock()
        ctx.memory.recall_facts_by_pattern = AsyncMock(return_value=[])
        ctx.memory.recall_episodes = AsyncMock(return_value=[])
        with patch.dict(sys.modules, {"app.context": ctx}):
            out = await loop._builtin_recall_memory({"query": "x"}, "u")
        assert "No matching facts found." in out["content"][0]["text"]


# ── MCP tool dispatch (lines 209-241) ───────────────────────────────────────


class TestMCPToolDispatch:
    @pytest.mark.asyncio
    async def test_mcp_tool_invoked_with_parsed_args(self):
        loop, router, mcp, ws = _make_loop(
            complete_side_effect=[
                _msg_response(
                    content="Let me check.",
                    tool_calls=[_tool_call("web_search", '{"q": "klukai"}', "c9")],
                ),
                _msg_response(content="Here is what I found."),
            ]
        )
        mcp.invoke_tool = AsyncMock(return_value=_mcp_text("search results here"))
        with patch.object(al, "get_tool_schemas", AsyncMock(return_value=[{"t": 1}])), patch.object(
            al, "get_builtin_tools", return_value=[]
        ):
            res = await loop.run("sys", [{"role": "user", "content": "search"}])
        mcp.invoke_tool.assert_awaited_once_with("web_search", {"q": "klukai"})
        assert res.tools_used == ["web_search"]
        assert res.response == "Here is what I found."
        # second LLM call must include the threaded tool result message
        second_msgs = router.complete_local.await_args_list[1].args[1]
        roles = [m["role"] for m in second_msgs]
        assert "assistant" in roles and "tool" in roles
        tool_msg = [m for m in second_msgs if m["role"] == "tool"][0]
        assert tool_msg["tool_call_id"] == "c9"
        assert "search results here" in tool_msg["content"]
        # status message uses TOOL_STATUS map
        ws.send_tool_use.assert_any_await("default", "web_search", "calling")
        ws.send_tool_use.assert_any_await("default", "web_search", "done")

    @pytest.mark.asyncio
    async def test_tool_timeout_records_error_and_recovers_session(self):
        import asyncio

        loop, router, mcp, ws = _make_loop(
            complete_side_effect=[
                _msg_response(tool_calls=[_tool_call("slow_tool")]),
                _msg_response(content="Done despite timeout."),
            ]
        )
        mcp.invoke_tool = AsyncMock(side_effect=asyncio.TimeoutError())
        with patch.object(al, "get_tool_schemas", AsyncMock(return_value=[{"t": 1}])), patch.object(
            al, "get_builtin_tools", return_value=[]
        ):
            res = await loop.run("sys", [{"role": "user", "content": "go"}])
        # timeout -> error result threaded; session re-init attempted
        mcp._initialize_session.assert_awaited()
        tool_msg = [
            m
            for m in router.complete_local.await_args_list[1].args[1]
            if m["role"] == "tool"
        ][0]
        assert "timed out" in tool_msg["content"]
        assert res.response == "Done despite timeout."

    @pytest.mark.asyncio
    async def test_tool_timeout_session_recovery_failure_swallowed(self):
        """Timeout AND the recovery _initialize_session() raises -> swallowed (220-221)."""
        import asyncio

        loop, router, mcp, ws = _make_loop(
            complete_side_effect=[
                _msg_response(tool_calls=[_tool_call("slow_tool")]),
                _msg_response(content="recovered anyway"),
            ]
        )
        mcp.invoke_tool = AsyncMock(side_effect=asyncio.TimeoutError())
        mcp._initialize_session = AsyncMock(side_effect=RuntimeError("reinit failed"))
        with patch.object(al, "get_tool_schemas", AsyncMock(return_value=[{"t": 1}])), patch.object(
            al, "get_builtin_tools", return_value=[]
        ):
            res = await loop.run("sys", [{"role": "user", "content": "go"}])
        mcp._initialize_session.assert_awaited()  # attempted despite failing
        assert res.response == "recovered anyway"

    @pytest.mark.asyncio
    async def test_tool_generic_exception_records_error(self):
        loop, router, mcp, ws = _make_loop(
            complete_side_effect=[
                _msg_response(tool_calls=[_tool_call("bad_tool")]),
                _msg_response(content="recovered"),
            ]
        )
        mcp.invoke_tool = AsyncMock(side_effect=RuntimeError("explode"))
        with patch.object(al, "get_tool_schemas", AsyncMock(return_value=[{"t": 1}])), patch.object(
            al, "get_builtin_tools", return_value=[]
        ):
            res = await loop.run("sys", [{"role": "user", "content": "go"}])
        tr = [s for s in res.steps if s.type == "tool_result"][0]
        assert tr.tool_result == {"error": "explode"}
        assert res.response == "recovered"

    @pytest.mark.asyncio
    async def test_invalid_json_args_default_to_empty_dict(self):
        loop, router, mcp, ws = _make_loop(
            complete_side_effect=[
                _msg_response(tool_calls=[_tool_call("web_search", "{not valid json")]),
                _msg_response(content="ok"),
            ]
        )
        mcp.invoke_tool = AsyncMock(return_value=_mcp_text("x"))
        with patch.object(al, "get_tool_schemas", AsyncMock(return_value=[{"t": 1}])), patch.object(
            al, "get_builtin_tools", return_value=[]
        ):
            await loop.run("sys", [{"role": "user", "content": "go"}])
        mcp.invoke_tool.assert_awaited_once_with("web_search", {})

    @pytest.mark.asyncio
    async def test_unknown_tool_uses_generic_status(self):
        loop, router, mcp, ws = _make_loop(
            complete_side_effect=[
                _msg_response(tool_calls=[_tool_call("calculator")]),
                _msg_response(content="done"),
            ]
        )
        mcp.invoke_tool = AsyncMock(return_value=_mcp_text("42"))
        with patch.object(al, "get_tool_schemas", AsyncMock(return_value=[{"t": 1}])), patch.object(
            al, "get_builtin_tools", return_value=[]
        ):
            await loop.run("sys", [{"role": "user", "content": "go"}])
        # generic "Using {tool}..." status sent (not in TOOL_STATUS map)
        sent = [c.args[1] for c in ws.send_thinking.await_args_list]
        assert "Using calculator..." in sent


# ── Completion failure -> streaming fallback (lines 143-162) ────────────────


class TestStreamingFallback:
    @pytest.mark.asyncio
    async def test_completion_raises_streaming_fallback_succeeds(self):
        loop, router, mcp, ws = _make_loop(
            complete_side_effect=RuntimeError("LLM 500")
        )

        async def _stream(sp, msgs, cfg):
            yield "fallback "
            yield "text"

        router.stream = _stream
        with patch.object(al, "get_tool_schemas", AsyncMock(return_value=[])), patch.object(
            al, "get_builtin_tools", return_value=[]
        ):
            res = await loop.run("sys", [{"role": "user", "content": "hi"}])
        assert res.response == "fallback text"
        # streaming-fallback status surfaced
        sent = [c.args[1] for c in ws.send_thinking.await_args_list]
        assert "Switching to direct response..." in sent

    @pytest.mark.asyncio
    async def test_completion_and_stream_both_fail_disrupted_message(self):
        loop, router, mcp, ws = _make_loop(
            complete_side_effect=RuntimeError("LLM 500")
        )

        async def _stream(sp, msgs, cfg):
            raise RuntimeError("stream dead")
            yield  # pragma: no cover

        router.stream = _stream
        with patch.object(al, "get_tool_schemas", AsyncMock(return_value=[])), patch.object(
            al, "get_builtin_tools", return_value=[]
        ):
            res = await loop.run("sys", [{"role": "user", "content": "hi"}])
        assert "Communications disrupted" in res.response

    @pytest.mark.asyncio
    async def test_streaming_fallback_empty_then_break_to_synthesis(self):
        """Stream yields nothing -> fall to break, then forced synthesis runs."""
        # complete_local: first call raises; forced-synthesis call returns text.
        calls = {"n": 0}

        async def _complete(sp, msgs, cfg, tools=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return _msg_response(content="synth briefing")

        loop, router, mcp, ws = _make_loop()
        router.complete_local = AsyncMock(side_effect=_complete)

        async def _empty_stream(sp, msgs, cfg):
            if False:  # pragma: no cover
                yield ""
            return

        router.stream = _empty_stream
        with patch.object(al, "get_tool_schemas", AsyncMock(return_value=[])), patch.object(
            al, "get_builtin_tools", return_value=[]
        ):
            res = await loop.run("sys", [{"role": "user", "content": "hi"}])
        # The disrupted-message break sets a response, so synthesis is NOT needed;
        # but stream produced nothing, so we expect the disrupted fallback text.
        assert "Communications disrupted" in res.response


# ── Forced synthesis after tool loop (lines 250-272) ────────────────────────


class TestForcedSynthesis:
    @pytest.mark.asyncio
    async def test_synthesis_runs_when_loop_ends_without_response(self):
        """All iterations keep calling tools -> loop exhausts -> forced synthesis."""
        # Every iteration returns a tool call (never a plain response), so after
        # MAX_ITERATIONS the loop exits with empty response and synthesizes.
        responses = [
            _msg_response(tool_calls=[_tool_call("web_search", tc_id=f"c{i}")])
            for i in range(al.MAX_ITERATIONS)
        ]
        responses.append(_msg_response(content="Final synthesized briefing."))
        loop, router, mcp, ws = _make_loop(complete_side_effect=responses)
        mcp.invoke_tool = AsyncMock(return_value=_mcp_text("data"))
        with patch.object(al, "get_tool_schemas", AsyncMock(return_value=[{"t": 1}])), patch.object(
            al, "get_builtin_tools", return_value=[]
        ):
            res = await loop.run("sys", [{"role": "user", "content": "go"}])
        assert res.iterations == al.MAX_ITERATIONS
        # synthesis call has tools=None
        last_call = router.complete_local.await_args_list[-1]
        assert last_call.kwargs["tools"] is None
        assert res.response == "Final synthesized briefing."
        sent = [c.args[1] for c in ws.send_thinking.await_args_list]
        assert "Compiling briefing..." in sent

    @pytest.mark.asyncio
    async def test_synthesis_failure_falls_to_default_message(self):
        responses = [
            _msg_response(tool_calls=[_tool_call("web_search", tc_id=f"c{i}")])
            for i in range(al.MAX_ITERATIONS)
        ]

        # The synthesis call (MAX_ITERATIONS+1 th) raises.
        async def _complete(sp, msgs, cfg, tools=None):
            if tools is None:  # synthesis call
                raise RuntimeError("synth failed")
            return responses.pop(0)

        loop, router, mcp, ws = _make_loop()
        router.complete_local = AsyncMock(side_effect=_complete)
        mcp.invoke_tool = AsyncMock(return_value=_mcp_text("data"))
        with patch.object(al, "get_tool_schemas", AsyncMock(return_value=[{"t": 1}])), patch.object(
            al, "get_builtin_tools", return_value=[]
        ):
            res = await loop.run("sys", [{"role": "user", "content": "go"}])
        assert "synthesis failed" in res.response

    @pytest.mark.asyncio
    async def test_synthesis_blank_content_falls_to_default(self):
        responses = [
            _msg_response(tool_calls=[_tool_call("web_search", tc_id=f"c{i}")])
            for i in range(al.MAX_ITERATIONS)
        ]
        responses.append(_msg_response(content="   "))  # whitespace-only synth
        loop, router, mcp, ws = _make_loop(complete_side_effect=responses)
        mcp.invoke_tool = AsyncMock(return_value=_mcp_text("data"))
        with patch.object(al, "get_tool_schemas", AsyncMock(return_value=[{"t": 1}])), patch.object(
            al, "get_builtin_tools", return_value=[]
        ):
            res = await loop.run("sys", [{"role": "user", "content": "go"}])
        assert "synthesis failed" in res.response


# ── Wall-clock timeout guard (lines 133-136) ────────────────────────────────


class TestTimeoutGuard:
    @pytest.mark.asyncio
    async def test_elapsed_over_budget_breaks_into_synthesis(self):
        """Clock jumps past TIMEOUT_SECONDS before iter 1 -> loop breaks."""
        loop, router, mcp, ws = _make_loop(
            complete_side_effect=[_msg_response(content="should-not-run")]
        )
        # forced-synthesis (after break) returns the only real content
        router.complete_local = AsyncMock(
            return_value=_msg_response(content="post-timeout synth")
        )
        # start_time=1000; first iteration sees elapsed = 1000+200-1000 = 200 > 150
        clock = iter([1000.0, 1000.0 + al.TIMEOUT_SECONDS + 50])

        def _mono():
            try:
                return next(clock)
            except StopIteration:
                return 1000.0 + al.TIMEOUT_SECONDS + 50

        with patch.object(al.time, "monotonic", _mono), patch.object(
            al, "get_tool_schemas", AsyncMock(return_value=[])
        ), patch.object(al, "get_builtin_tools", return_value=[]):
            res = await loop.run("sys", [{"role": "user", "content": "hi"}])
        # The per-iteration completion was never called (broke before it);
        # only the forced-synthesis completion ran.
        assert res.response == "post-timeout synth"
        assert res.iterations == 1


# ── _extract_tool_text helper (lines 277-303) ───────────────────────────────


class TestExtractToolText:
    def test_list_of_text_items_joined_and_html_stripped(self):
        out = _extract_tool_text(
            {"content": [{"type": "text", "text": "<b>Hello</b>   world"}]}
        )
        assert out == "Hello world"

    def test_item_with_text_key_no_type(self):
        out = _extract_tool_text({"content": [{"text": "plain"}]})
        assert out == "plain"

    def test_list_of_raw_strings(self):
        out = _extract_tool_text({"content": ["a", "b"]})
        assert out == "a b"

    def test_string_content_returned_directly(self):
        assert _extract_tool_text({"content": "just a string"}) == "just a string"

    def test_error_key_formatted(self):
        assert _extract_tool_text({"error": "boom"}) == "Error: boom"

    def test_empty_list_content_with_error_falls_through(self):
        # content=[] (no texts) -> not returned; error branch handles it
        assert _extract_tool_text({"content": [], "error": "x"}) == "Error: x"

    def test_unknown_shape_stringified(self):
        out = _extract_tool_text({"weird": 123})
        assert "weird" in out


# ── Dataclasses sanity (AgentResult/AgentStep defaults) ─────────────────────


class TestDataclasses:
    def test_agent_result_defaults(self):
        r = AgentResult(response="hi")
        assert r.steps == [] and r.tools_used == [] and r.iterations == 0
        assert r.model == al.LOCAL_AGENT

    def test_agent_step_optional_fields(self):
        s = AgentStep(type="tool_call", content="x", tool_name="t", tool_args={"a": 1})
        assert s.tool_name == "t" and s.tool_args == {"a": 1}
