"""Regressions for one chat turn: follow-ups, cut-off streams, tool synthesis."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from starlette.websockets import WebSocketState

from app import chat, chat_handlers
from app.agent_loop import AgentLoop
from app.chat_reply import WARMUP_NOTICE_S, ChatReplyRuntime
from app.helpers import fix_narration
from app.llm_router import FAILURE_SENTINEL, LLMConfig, LLMStreamFailed
from app.memory import MemoryManager
from app.models import SessionState
from tests.test_agent_loop_coverage import _mcp_text, _msg_response, _schemas, _tool_call
from tests.test_chat_coverage import (
    _FakeConn,
    _FakeWebSocket,
    _capture_endpoint,
    _fake_ws_manager,
)
from tests.test_chat_handlers_coverage import _drain_tasks, _fresh_session, _patched_pipeline
from tests.test_session_authority import StoredRedis


async def _stop_extract_runners() -> None:
    tasks = [task for task in chat_handlers._TURN_EXTRACT_RUNNER.values() if not task.done()]
    for task in tasks:
        task.cancel()
    chat_handlers._TURN_EXTRACT_QUEUE.clear()
    chat_handlers._TURN_EXTRACT_GEN.clear()
    chat_handlers._TURN_EXTRACT_RUNNER.clear()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture(autouse=True)
async def _isolated_extract_state():
    await _stop_extract_runners()
    yield
    await _stop_extract_runners()


async def test_later_turn_waits_out_the_previous_extraction():
    """A new ChatFollowups is built per message. Its queue must still be the
    user's queue, or the first turn's extraction runs while he is replying."""
    first = "Remember the convoy from last night"
    second = "The ridge was quiet after midnight"
    release = asyncio.Event()
    started: list[str] = []
    # The chat harness replaces asyncio.sleep with a finished mock, which never
    # gives the extraction task a turn. Yield once for real instead.
    real_module_sleep = asyncio.sleep

    async def yield_once(_delay: float = 0):
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(future.set_result, None)
        await future

    async def extract(content, *_args, **_kwargs):
        started.append(content)
        if content == first:
            await release.wait()

    with (
        _patched_pipeline() as ns,
        patch(
            "app.chat_handlers.picture_intent.resolve",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.conversation_continuity.load_recent_thread",
            new=AsyncMock(return_value=[]),
        ),
    ):
        asyncio.sleep = yield_once
        ns.background_extraction.side_effect = extract
        session = _fresh_session()
        try:
            await chat_handlers._handle_message(first, session, "alice")
            for _ in range(10):
                if started:
                    break
                await yield_once()
            assert started == [first]
            await chat_handlers._handle_message(second, session, "alice")
            for _ in range(10):
                await yield_once()
            assert started == [first], (
                "second turn started extraction while the first was still running"
            )
            release.set()
            await _drain_tasks()
        finally:
            asyncio.sleep = real_module_sleep
    assert started == [first, second]


async def test_cut_off_reply_is_not_published_as_finished_text():
    sent: list[tuple[str, object]] = []

    async def stream(_system, _messages, _config):
        yield "(You look up.)"
        raise LLMStreamFailed("cut")

    ws = MagicMock()
    ws.send_thinking = AsyncMock()
    ws.send_token = AsyncMock(side_effect=lambda _u, text: sent.append(("token", text)))
    ws.send = AsyncMock(side_effect=lambda _u, msg: sent.append((msg["type"], msg.get("message"))))
    ws.send_done = AsyncMock(
        side_effect=lambda *_a, **kwargs: sent.append(("done", kwargs.get("final_text")))
    )
    runtime = ChatReplyRuntime(
        MagicMock(
            route=AsyncMock(return_value=LLMConfig(provider="offline-test", model="m")),
            stream=stream,
        ),
        None,
        ws,
        None,
        fix_narration,
        FAILURE_SENTINEL.split(",", 1)[0],
    )
    reply = await runtime.direct(
        "status", SessionState(conversation_id="c"), "persona", [], "u", "m1", 0.0
    )
    assert reply.failed
    assert reply.text == "(I look up.)"
    assert ("done", None) in sent
    assert ("done", "(I look up.)") not in sent
    assert [kind for kind, _ in sent].index("error") < [kind for kind, _ in sent].index("done")


async def test_empty_reply_still_carries_the_failure_cue():
    async def stream(_system, _messages, _config):
        if False:
            yield ""

    ws = MagicMock()
    ws.send_thinking = AsyncMock()
    ws.send_token = AsyncMock()
    ws.send_done = AsyncMock()
    runtime = ChatReplyRuntime(
        MagicMock(
            route=AsyncMock(return_value=LLMConfig(provider="offline-test", model="m")),
            stream=stream,
        ),
        None,
        ws,
        None,
        fix_narration,
        FAILURE_SENTINEL.split(",", 1)[0],
    )
    reply = await runtime.direct(
        "status", SessionState(conversation_id="c"), "persona", [], "u", "m1", 0.0
    )
    assert reply.failed
    assert reply.text == FAILURE_SENTINEL
    assert ws.send_done.await_args.kwargs["final_text"] == FAILURE_SENTINEL


async def test_offline_route_puts_the_failure_cue_on_done():
    ws = MagicMock()
    ws.send_thinking = AsyncMock()
    ws.send_token = AsyncMock()
    ws.send_done = AsyncMock()
    runtime = ChatReplyRuntime(
        MagicMock(route=AsyncMock(side_effect=RuntimeError("no model"))),
        None,
        ws,
        None,
        lambda text: text,
        FAILURE_SENTINEL.split(",", 1)[0],
    )
    reply = await runtime.direct(
        "status", SessionState(conversation_id="c"), "persona", [], "u", "m1", 0.0
    )
    assert reply.failed and reply.text == FAILURE_SENTINEL
    ws.send_token.assert_awaited_once_with("u", FAILURE_SENTINEL)
    assert ws.send_done.await_args.kwargs["final_text"] == FAILURE_SENTINEL
    assert ws.send_done.await_args.args[2] == "offline"


async def test_cut_off_stream_does_not_let_warmup_speak_during_the_error(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []
    real_sleep = asyncio.sleep

    async def sleep(delay):
        if delay == WARMUP_NOTICE_S:
            return
        await real_sleep(0)

    monkeypatch.setattr("app.chat_reply.asyncio.sleep", sleep)

    class Router:
        async def route(self, *_args, **_kwargs):
            return LLMConfig(provider="anthropic", model="m", base_url="http://127.0.0.1")

        async def stream(self, *_args, **_kwargs):
            await entered.wait()
            raise LLMStreamFailed("cut")
            yield ""  # pragma: no cover

    class Socket:
        async def send_thinking(self, _user_id, text):
            if str(text).startswith("One moment"):
                entered.set()
                await release.wait()
                order.append("warmup")

        async def send(self, _user_id, msg):
            if isinstance(msg, dict) and msg.get("type") == "error":
                order.append("error")
                release.set()
                await real_sleep(0)

        async def send_token(self, *_args):
            return None

        async def send_done(self, *_args, **_kwargs):
            order.append("done")

    runtime = ChatReplyRuntime(
        Router(),
        None,
        Socket(),
        None,
        lambda text: text,
        FAILURE_SENTINEL.split(",", 1)[0],
    )
    reply = await runtime.direct(
        "hello", SessionState(conversation_id="c"), "persona", [], "u", "m", 0.0
    )
    assert reply.failed
    assert "warmup" not in order
    assert order[0] == "error"


async def test_synthesis_failure_is_not_a_successful_answer():
    import app.agent_loop as agent_loop

    calls = {"n": 0}

    async def complete(_system, _messages, _config, tools=None, allow_tools=True):
        calls["n"] += 1
        if not allow_tools:
            raise RuntimeError("synth failed")
        return _msg_response(tool_calls=[_tool_call("web_search", tc_id=f"c{calls['n']}")])

    router = MagicMock()
    router.route = AsyncMock(
        return_value=LLMConfig(
            provider="anthropic",
            model=agent_loop.CLAUDE_SECONDARY,
            base_url="https://api.anthropic.com/v1/messages",
        )
    )
    router.complete_chat = AsyncMock(side_effect=complete)
    mcp = MagicMock()
    mcp.invoke_tool = AsyncMock(return_value=_mcp_text("data"))
    mcp._initialize_session = AsyncMock()
    ws = MagicMock()
    ws.send_thinking = AsyncMock()
    ws.send_tool_use = AsyncMock()
    loop = AgentLoop(router, mcp, ws)
    with (
        patch.object(agent_loop, "get_tool_schemas", AsyncMock(return_value=_schemas("web_search"))),
        patch.object(agent_loop, "get_builtin_tools", return_value=[]),
    ):
        result = await loop.run(
            "sys", [{"role": "user", "content": "go"}], interactive=True
        )
    assert result.response.startswith("Communications disrupted")
    assert "synthesis failed" in result.response

    completed = Mock()
    delivery = MagicMock()
    delivery.send_token = AsyncMock()
    delivery.send_done = AsyncMock()
    runtime = ChatReplyRuntime(
        None,
        None,
        delivery,
        MagicMock(return_value=MagicMock(run=AsyncMock(return_value=result))),
        fix_narration,
        FAILURE_SENTINEL.split(",", 1)[0],
        on_completed=completed,
    )
    reply = await runtime.agent("persona", [], "owner", "message", 0.0)
    assert reply.failed
    completed.assert_not_called()
    assert delivery.send_done.await_args.kwargs["final_text"] is None


async def test_closed_socket_receive_is_a_disconnect_not_a_crash():
    endpoint, _path = _capture_endpoint()
    sock = _FakeWebSocket(token="good")

    async def boom():
        raise RuntimeError('WebSocket is not connected. Need to call "accept" first.')

    sock.receive_text = AsyncMock(side_effect=boom)
    assert sock.application_state == WebSocketState.CONNECTED
    manager = MemoryManager()
    manager._redis = StoredRedis()
    await manager.save_session(chat.session_id("u1"), SessionState(conversation_id="c1"))
    ws = _fake_ws_manager()
    proactive = MagicMock()
    proactive.mission_active = False
    with (
        patch("app.chat.ws", ws),
        patch("app.chat.memory", manager),
        patch("app.chat.proactive", proactive),
        patch("app.auth.get_user_from_token", AsyncMock(return_value="u1")),
        patch("app.chat.get_conn", return_value=_FakeConn(("composed",))),
        patch("app.chat._maybe_reflect_on_return", AsyncMock()),
        patch("app.chat._maybe_oath_on_connect", AsyncMock()),
        patch("app.rituals.on_connect", AsyncMock()),
        patch("app.wardrobe.warm_today", AsyncMock()),
    ):
        await endpoint(sock)
    ws.disconnect.assert_awaited()
    assert sock.receive_text.await_count == 1